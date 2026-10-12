"""Five offline acceptance groups. Every external command is mocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/hunt-free-vm.yml'
TEXT = WORKFLOW.read_text()
SOURCE = '\n'.join(line[10:] for line in TEXT.split("          python3 - <<'PY'\n", 1)[1].rsplit('\n          PY', 1)[0].splitlines())
ARM = 'VM.Standard.A1.Flex'
AMD = 'VM.Standard.E2.1.Micro'
REGION = 'ap-kulai-2'
TENANCY = 'ocid1.tenancy.oc1..mock'
COMPARTMENT = 'ocid1.compartment.oc1..mock'
AD = 'mock:AP-KULAI-2-AD-1'
IMAGE = 'ocid1.image.oc1.ap-kulai-2.mock-arm'
IMAGES = {ARM: IMAGE, AMD: 'ocid1.image.oc1.ap-kulai-2.mock-amd'}
SENSITIVE = 'MOCK_SENSITIVE_NEVER_RELAY'


def instance(shape=ARM, state='PROVISIONING'):
    return {'id': 'ocid1.instance.oc1.ap-kulai-2.mock', 'shape': shape, 'lifecycle-state': state}


def service_error(status, code, message):
    return subprocess.CompletedProcess([], 1, '', 'ServiceError:\n' + json.dumps(
        {'status': status, 'code': code, 'message': message, 'credentials': SENSITIVE}))


CAPACITY = service_error(500, 'InternalError', 'Out of host capacity.')


class MockCommands:
    def __init__(self, launches=(), target_lists=((),), disable_status=204, query_error=None, overrides=None):
        self.launches = list(launches)
        self.target_lists = list(target_lists)
        self.disable_status = disable_status
        self.query_error = query_error
        self.overrides = overrides or {}
        self.calls = []
        self.creates = []
        self.notifications = []
        self.disables = []
        self.secret_files = []
        self.target_queries = 0
        self.events = []
        self.unexpected = []

    def __call__(self, command, **kwargs):
        try:
            return self.dispatch(command, **kwargs)
        except AssertionError:
            self.unexpected.append('mock contract violation')
            raise

    def dispatch(self, command, **kwargs):
        self.calls.append(command)
        self.assert_capture(kwargs)
        if command[1:4] == ['-m', 'pip', 'install']:
            self.events.append('install')
            assert command[-1] == 'oci-cli==3.94.2'
            for key in ('OCI_CLI_KEY_FILE', 'HUNT_SSH_FILE'):
                path = Path(os.environ[key])
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
                assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
                self.secret_files.append(path)
            assert 'OCI_CLI_KEY_CONTENT' not in os.environ
            return subprocess.CompletedProcess(command, 0, '', '')
        if command[0] == 'curl':
            assert command[1:3] == ['--config', '-']
            config = kwargs['input']
            assert SENSITIVE not in ' '.join(command)
            if '/disable' in config:
                self.disables.append(config)
                self.events.append('disable')
                if self.disable_status == 'timeout':
                    raise subprocess.TimeoutExpired(command, 25)
                assert '/actions/workflows/hunt-free-vm.yml/disable' in config
                assert command[command.index('--request') + 1] == 'PUT'
                return subprocess.CompletedProcess(command, 0, '\n' + str(self.disable_status), '')
            assert '/sendMessage' in config
            assert command[command.index('--request') + 1] == 'POST'
            payload = next(line[7:] for line in config.splitlines() if line.startswith('data = '))
            message = json.loads(json.loads(payload))['text']
            assert SENSITIVE not in message
            assert 'ocid1.' not in message
            self.notifications.append(message)
            self.events.append('notify')
            response = self.overrides.get('notify_responses', [])
            if response:
                outcome = response.pop(0)
                if outcome == 'timeout':
                    raise subprocess.TimeoutExpired(command, 25)
                return subprocess.CompletedProcess(command, 0, outcome, '')
            return subprocess.CompletedProcess(command, 0, '{"ok":true}\n200', '')
        if command[0] != 'oci':
            self.unexpected.append(command[0])
            raise AssertionError('Unexpected external command')
        assert command[command.index('--region') + 1] == REGION
        assert '--no-retry' in command
        assert '--debug' not in command
        args = command[10:]
        # Exact prefix: oci --region R --no-retry --connection-timeout 10 --read-timeout 30 --output json
        operation = tuple(args[:3])
        if args[-1:] == ['--all']:
            assert '--limit' not in args
        elif operation[2] == 'list':
            raise AssertionError('List must fetch every page')
        if operation == ('compute', 'instance', 'list'):
            compartment = args[args.index('--compartment-id') + 1]
            if compartment == COMPARTMENT:
                self.target_queries += 1
                if self.query_error:
                    return self.query_error
                # Simulate the CLI --all aggregation of multiple service pages.
                index = min(self.target_queries - 1, len(self.target_lists) - 1)
                outcome = self.target_lists[index]
                self.events.append('query')
                if isinstance(outcome, Exception):
                    raise outcome
                if isinstance(outcome, subprocess.CompletedProcess):
                    return outcome
                data = list(outcome)
            else:
                data = self.overrides.get('other_instances', [])
        elif operation == ('iam', 'region-subscription', 'list'):
            data = [{'region-name': self.overrides.get('home', REGION), 'is-home-region': True}]
        elif operation == ('iam', 'availability-domain', 'list'):
            self.events.append('ad')
            if self.overrides.get('ad_failure'):
                return service_error(429, 'TooManyRequests', 'mock discovery failure')
            data = [{'name': AD}]
        elif operation == ('network', 'subnet', 'get'):
            data = {'id': os.environ['OCI_SUBNET_ID'], 'lifecycle-state': 'AVAILABLE', 'availability-domain': None,
                    'prohibit-public-ip-on-vnic': False}
            data.update(self.overrides.get('subnet', {}))
            if self.overrides.get('subnet_missing_public_ip_field'):
                data.pop('prohibit-public-ip-on-vnic')
        elif operation == ('iam', 'compartment', 'list'):
            assert '--include-root' in args
            assert args[args.index('--compartment-id-in-subtree') + 1] == 'true'
            assert args[args.index('--access-level') + 1] == 'ANY'
            data = [{'id': TENANCY, 'lifecycle-state': 'ACTIVE'}, {'id': COMPARTMENT, 'lifecycle-state': 'ACTIVE'}]
        elif operation in (('bv', 'volume', 'list'), ('bv', 'boot-volume', 'list')):
            data = []
            if operation == ('bv', 'boot-volume', 'list') and args[args.index('--compartment-id') + 1] == TENANCY:
                data = self.overrides.get('volumes', [])
        elif operation in (('compute', 'image', 'list'), ('compute', 'image', 'get')):
            shape = args[args.index('--shape') + 1] if '--shape' in args else next(k for k, v in IMAGES.items() if v == args[args.index('--image-id') + 1])
            self.events.append('image:' + shape)
            if self.overrides.get('image_failure') == shape:
                return service_error(self.overrides.get('image_failure_status', 401), 'MockDiscoveryFailure', 'mock discovery failure')
            data = {'id': IMAGES[shape], 'compartment-id': None, 'operating-system': 'Canonical Ubuntu',
                    'operating-system-version': '24.04', 'lifecycle-state': 'AVAILABLE', 'size-in-mbs': 25600,
                    'display-name': 'Canonical-Ubuntu-24.04' + ('-aarch64' if shape == ARM else '') + '-2026.09.18-0'}
            data.update(self.overrides.get('image', {}))
            if operation[2] == 'list':
                data = [data]
        elif operation == ('compute', 'image-shape-compatibility-entry', 'get'):
            data = {'image-id': args[args.index('--image-id') + 1], 'shape': args[args.index('--shape-name') + 1]}
        elif operation == ('compute', 'instance', 'launch'):
            self.creates.append(command)
            self.events.append('launch')
            assert self.events.count('image:' + ARM) == 1
            assert self.events.count('image:' + AMD) == 1
            assert self.events.index('image:' + AMD) < self.events.index('query')
            shape = args[args.index('--shape') + 1]
            assert shape in (ARM, AMD)
            assert args[args.index('--availability-domain') + 1] == AD
            assert args[args.index('--subnet-id') + 1] == os.environ['OCI_SUBNET_ID']
            assert args[args.index('--compartment-id') + 1] == COMPARTMENT
            source = json.loads(args[args.index('--source-details') + 1])
            assert source == {'sourceType': 'image', 'imageId': IMAGES[shape], 'bootVolumeSizeInGBs': 50, 'bootVolumeVpusPerGB': 10}
            assert args[args.index('--assign-public-ip') + 1] == 'true'
            assert args[args.index('--capacity-reservation-id') + 1] == ''
            assert '--wait-for-state' not in args
            if shape == ARM:
                assert json.loads(args[args.index('--shape-config') + 1]) == {'ocpus': 2, 'memoryInGBs': 12}
            else:
                assert '--shape-config' not in args
            assert self.launches, 'Unexpected additional creation'
            outcome = self.launches.pop(0)
            if outcome == 'timeout':
                raise subprocess.TimeoutExpired(command, 90)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, subprocess.CompletedProcess):
                return outcome
            data = outcome
        else:
            self.unexpected.append(operation)
            raise AssertionError('Unmocked OCI operation')
        return subprocess.CompletedProcess(command, 0, json.dumps({'data': data}), '')

    @staticmethod
    def assert_capture(kwargs):
        assert kwargs.get('capture_output') is True
        assert kwargs.get('text') is True
        assert kwargs.get('timeout') in (25, 60, 90, 180)


def run_case(mock, environment=None):
    env = {
        'OCI_CLI_TENANCY': TENANCY, 'OCI_CLI_USER': 'mock-user', 'OCI_CLI_FINGERPRINT': 'mock-fingerprint',
        'OCI_CLI_KEY_CONTENT': SENSITIVE, 'OCI_COMPARTMENT_ID': COMPARTMENT,
        'OCI_SUBNET_ID': 'ocid1.subnet.oc1.ap-kulai-2.mock', 'SSH_PUBLIC_KEY': 'ssh-ed25519 bW9jaw==',
        'TG_BOT_TOKEN': SENSITIVE, 'TG_CHAT_ID': 'mock-chat', 'GITHUB_TOKEN': SENSITIVE,
        'GITHUB_REPOSITORY': 'mock-owner/mock-repo',
        'GITHUB_WORKFLOW_REF': 'mock-owner/mock-repo/.github/workflows/hunt-free-vm.yml@refs/heads/main',
    }
    env.update(environment or {})
    output = io.StringIO()
    with tempfile.TemporaryDirectory(prefix='oci-offline-') as home:
        env.update({'HOME': home, 'TMPDIR': home, 'OCI_CLI_CONFIG_FILE': str(Path(home) / 'absent-config')})
        # Force tempfile isolation as well as credential/environment isolation.
        with patch.object(tempfile, 'tempdir', home), patch.dict(os.environ, env, clear=True), \
                patch('subprocess.run', side_effect=mock), contextlib.redirect_stdout(output):
            try:
                exec(compile(SOURCE, str(WORKFLOW), 'exec'), {'__name__': '__main__'})
                status = 0
            except SystemExit as exit_result:
                status = exit_result.code
        assert not mock.unexpected, 'Unknown external call was blocked'
        assert SENSITIVE not in output.getvalue()
        assert all(not path.exists() for path in mock.secret_files), 'Temporary credentials were not removed'
    return status, output.getvalue()


class OfflineAcceptance(unittest.TestCase):
    def assert_counts(self, mock, creates, notices, disables):
        self.assertEqual((len(mock.creates), len(mock.notifications), len(mock.disables)), (creates, notices, disables))
        if disables:
            expected = ['notify', 'disable'] + (['notify'] if notices == 2 else [])
            self.assertEqual([event for event in mock.events if event in ('notify', 'disable')], expected)

    def test_1_arm_success(self):
        mock = MockCommands(launches=[instance()])
        status, _ = run_case(mock)
        self.assertEqual(status, 0)
        self.assert_counts(mock, 1, 1, 1)
        self.assertEqual(mock.target_queries, 2)
        self.assertEqual(mock.events[-2:], ['notify', 'disable'])
        self.assertIn('ARM 2 OCPU / 12 GB', mock.notifications[0])
        self.assertRegex(TEXT, r"cron: '\*/15 \* \* \* \*'")
        self.assertIn('cancel-in-progress: false', TEXT)
        self.assertIn('actions: write', TEXT)
        self.assertNotRegex(TEXT, r'(?m)^\s*(pull_request|pull_request_target|push):')
        self.assertNotIn('set -x', TEXT)
        self.assertEqual(len(list((ROOT / '.github/workflows').glob('*.yml'))), 1)
        self.assertEqual(len(list((ROOT / '.github/workflows').glob('*.yaml'))), 0)

    def test_2_arm_capacity_amd_success(self):
        mock = MockCommands(launches=[CAPACITY, instance(AMD)])
        self.assertEqual(run_case(mock)[0], 0)
        self.assert_counts(mock, 2, 1, 1)
        self.assertEqual([c[c.index('--shape') + 1] for c in mock.creates], [ARM, AMD])
        self.assertIn('AMD Micro fallback', mock.notifications[0])

    def test_3_all_capacity_silent(self):
        mock = MockCommands(launches=[CAPACITY, CAPACITY])
        status, output = run_case(mock)
        self.assertEqual(status, 0)
        self.assertEqual(output, '')
        self.assert_counts(mock, 2, 0, 0)

    def test_4_existing_instances(self):
        for shape in (ARM, AMD):
            for state in ('RUNNING', 'PROVISIONING'):
                with self.subTest(shape=shape, state=state):
                    mock = MockCommands(target_lists=[[instance(AMD, 'TERMINATED'), instance(shape, state)]])
                    self.assertEqual(run_case(mock)[0], 0)
                    self.assert_counts(mock, 0, 1, 1)
                    self.assertEqual(mock.target_queries, 1)
                    self.assertIn(shape, mock.notifications[0])
                    self.assertIn(state, mock.notifications[0])
                    self.assertEqual(mock.events[-2:], ['notify', 'disable'])
        with self.subTest(state='TERMINATING'):
            mock = MockCommands(target_lists=[[instance(state='TERMINATING')]])
            status, output = run_case(mock)
            self.assertEqual(status, 0)
            self.assert_counts(mock, 0, 1, 1)
            self.assertIn(ARM, mock.notifications[0])
            self.assertIn('TERMINATING', mock.notifications[0])
            self.assertEqual(mock.events[-2:], ['notify', 'disable'])

    def test_5_exception_branches(self):
        error = service_error(401, 'NotAuthenticated', 'mock authentication failure')
        with self.subTest(branch='query failed'):
            mock = MockCommands(query_error=error)
            self.assertEqual(run_case(mock)[0], 1)
            self.assert_counts(mock, 0, 1, 1)
        for final_inventory in ([], [instance()], 'failed'):
            with self.subTest(branch='timeout', final_inventory=final_inventory):
                mock = MockCommands(launches=['timeout'], target_lists=[[], [], final_inventory if final_inventory != 'failed' else []])
                original = mock.__class__.__call__
                if final_inventory == 'failed':
                    def failing_requery(command, **kwargs):
                        if 'instance' in command and 'list' in command and '--compartment-id' in command \
                                and command[command.index('--compartment-id') + 1] == COMPARTMENT and mock.target_queries >= 2:
                            mock.target_queries += 1
                            return error
                        return original(mock, command, **kwargs)
                    with patch.object(mock.__class__, '__call__', side_effect=failing_requery):
                        status, _ = run_case(mock)
                else:
                    status, _ = run_case(mock)
                self.assertEqual(status, 0 if final_inventory == [instance()] else 1)
                self.assert_counts(mock, 1, 1, 1)
                self.assertEqual(mock.target_queries, 3)
        for rejection in (error, service_error(403, 'NotAuthorizedOrNotFound', 'mock permission failure'),
                          service_error(400, 'LimitExceeded', 'mock quota exceeded')):
            with self.subTest(branch='create rejection', http_status=json.loads(rejection.stderr.split('\n', 1)[1])['status']):
                mock = MockCommands(launches=[rejection])
                self.assertEqual(run_case(mock)[0], 1)
                self.assert_counts(mock, 1, 1, 1)
                self.assertEqual(mock.target_queries, 2)
        with self.subTest(branch='unconfirmed success response'):
            mock = MockCommands(launches=[{}])
            self.assertEqual(run_case(mock)[0], 1)
            self.assert_counts(mock, 1, 1, 1)
            self.assertEqual(mock.target_queries, 3)
        with self.subTest(branch='query 429 silent'):
            mock = MockCommands(query_error=service_error(429, 'TooManyRequests', 'mock rate limit'))
            self.assertEqual(run_case(mock), (0, ''))
            self.assert_counts(mock, 0, 0, 0)
        with self.subTest(branch='429 silent'):
            mock = MockCommands(launches=[service_error(429, 'TooManyRequests', 'mock rate limit')])
            self.assertEqual(run_case(mock), (0, ''))
            self.assert_counts(mock, 1, 0, 0)
        with self.subTest(branch='disable requires 204'):
            mock = MockCommands(launches=[instance()], disable_status=200)
            self.assertEqual(run_case(mock)[0], 1)
            self.assert_counts(mock, 1, 2, 1)
            self.assertEqual(mock.notifications[1], 'OCI hunt v3.1: 自动禁用失败，请手动去 Actions 页 Disable workflow')
            self.assertEqual(mock.events[-3:], ['notify', 'disable', 'notify'])
        for overrides in (
                {'home': 'ap-singapore-1'},
                {'subnet_missing_public_ip_field': True},
                {'subnet': {'prohibit-public-ip-on-vnic': True}},
                {'subnet': {'prohibit-public-ip-on-vnic': None}},
                {'subnet': {'prohibit-public-ip-on-vnic': 'false'}},
                {'volumes': [{'size-in-gbs': 151, 'lifecycle-state': 'AVAILABLE'}]},
                {'other_instances': [instance()]},
                {'image': {'compartment-id': COMPARTMENT}},
                {'image': {'size-in-mbs': 60 * 1024}}):
            with self.subTest(branch='free eligibility/configuration', overrides=overrides):
                mock = MockCommands(overrides=overrides)
                self.assertEqual(run_case(mock)[0], 1)
                self.assert_counts(mock, 0, 1, 1)
        with self.subTest(branch='ambiguous 500 capacity wording'):
            mock = MockCommands(launches=[service_error(500, 'InternalError', 'Request failed; maybe out of host capacity')])
            self.assertEqual(run_case(mock)[0], 1)
            self.assert_counts(mock, 1, 1, 1)
        with self.subTest(branch='explicit image override'):
            mock = MockCommands(launches=[instance()])
            self.assertEqual(run_case(mock, {'IMAGE_ID_ARM': IMAGE})[0], 0)
            self.assert_counts(mock, 1, 1, 1)

    def test_6_disable_and_notification_failures(self):
        for disabled in (200, 202, 403, 500, 'timeout'):
            with self.subTest(disable=disabled):
                mock = MockCommands(launches=[instance()], disable_status=disabled)
                self.assertEqual(run_case(mock)[0], 1)
                self.assert_counts(mock, 1, 2, 1)
                self.assertEqual(mock.events[-3:], ['notify', 'disable', 'notify'])
                self.assertIn('ARM 2 OCPU / 12 GB', mock.notifications[0])
                self.assertEqual(mock.notifications[1], 'OCI hunt v3.1: 自动禁用失败，请手动去 Actions 页 Disable workflow')
        for response in ('{"ok":false}\n500', 'invalid-json\n200', 'timeout'):
            with self.subTest(initial_notification=response):
                mock = MockCommands(launches=[instance()], overrides={'notify_responses': [response]})
                status, output = run_case(mock)
                self.assertEqual(status, 1)
                self.assert_counts(mock, 1, 1, 1)
                self.assertEqual(mock.events[-2:], ['notify', 'disable'])
                self.assertIn('Telegram delivery failed', output)
        for responses in (['{"ok":true}\n200', 'timeout'], ['timeout', 'timeout']):
            with self.subTest(additional_notification=responses):
                mock = MockCommands(launches=[instance()], disable_status=403,
                                    overrides={'notify_responses': list(responses)})
                status, output = run_case(mock)
                self.assertEqual(status, 1)
                self.assert_counts(mock, 1, 2, 1)
                self.assertEqual(mock.events[-3:], ['notify', 'disable', 'notify'])
                self.assertIn('manually disable workflow', output)
                self.assertIn('Telegram delivery failed', output)
        with self.subTest(initial_exception_alert='disable failed'):
            mock = MockCommands(query_error=service_error(401, 'NotAuthenticated', 'mock'), disable_status=500)
            self.assertEqual(run_case(mock)[0], 1)
            self.assert_counts(mock, 0, 2, 1)
            self.assertEqual(mock.events[-3:], ['notify', 'disable', 'notify'])

    def test_7_discovery_before_queries_and_creation(self):
        mock = MockCommands(launches=[CAPACITY, instance(AMD)])
        self.assertEqual(run_case(mock)[0], 0)
        self.assertEqual(mock.events.count('image:' + ARM), 1)
        self.assertEqual(mock.events.count('image:' + AMD), 1)
        self.assertLess(mock.events.index('install'), mock.events.index('ad'))
        self.assertLess(mock.events.index('ad'), mock.events.index('image:' + ARM))
        self.assertLess(mock.events.index('image:' + ARM), mock.events.index('query'))
        self.assertLess(mock.events.index('image:' + AMD), mock.events.index('query'))
        self.assert_counts(mock, 2, 1, 1)
        for shape in (ARM, AMD):
            for status in (401, 429):
                with self.subTest(failed_image=shape, http_status=status):
                    mock = MockCommands(overrides={'image_failure': shape, 'image_failure_status': status})
                    self.assertEqual(run_case(mock)[0], 1)
                    self.assert_counts(mock, 0, 1, 1)
                    self.assertEqual(mock.target_queries, 0)
                    self.assertEqual(mock.events[-2:], ['notify', 'disable'])
        mock = MockCommands(overrides={'ad_failure': True})
        self.assertEqual(run_case(mock)[0], 1)
        self.assert_counts(mock, 0, 1, 1)
        self.assertEqual(mock.target_queries, 0)
        with self.subTest(distinct_overrides=True):
            mock = MockCommands(launches=[CAPACITY, instance(AMD)])
            self.assertEqual(run_case(mock, {'IMAGE_ID_ARM': IMAGES[ARM], 'IMAGE_ID_AMD': IMAGES[AMD]})[0], 0)
            self.assert_counts(mock, 2, 1, 1)

    def test_8_uncertain_creation_requery_rate_limit(self):
        for final in (service_error(429, 'TooManyRequests', 'mock'), OSError('mock connection failure'),
                      [instance(AMD, 'TERMINATING')]):
            with self.subTest(final_type=type(final).__name__):
                mock = MockCommands(launches=['timeout'], target_lists=[[], [], final])
                self.assertEqual(run_case(mock)[0], 0 if isinstance(final, list) else 1)
                self.assert_counts(mock, 1, 1, 1)
                self.assertEqual(mock.target_queries, 3)
                self.assertEqual(mock.events[-2:], ['notify', 'disable'])
                if isinstance(final, list):
                    self.assertIn(AMD, mock.notifications[0])
                    self.assertIn('TERMINATING', mock.notifications[0])

    def test_9_invalid_responses_fail_closed(self):
        invalid_json = subprocess.CompletedProcess([], 0, 'invalid-json', '')
        missing_data = subprocess.CompletedProcess([], 0, '{}', '')
        for query in (invalid_json, missing_data, [{}], [instance(state='UNKNOWN')], OSError('mock connection failure')):
            with self.subTest(query_type=type(query).__name__, query_variant=str(query)[:24]):
                mock = MockCommands(target_lists=[query])
                self.assertEqual(run_case(mock)[0], 1)
                self.assert_counts(mock, 0, 1, 1)
                self.assertEqual(mock.target_queries, 1)
        for launch in (invalid_json, missing_data, {}, OSError('mock connection failure')):
            with self.subTest(launch_type=type(launch).__name__):
                mock = MockCommands(launches=[launch])
                self.assertEqual(run_case(mock)[0], 1)
                self.assert_counts(mock, 1, 1, 1)
                self.assertEqual(mock.target_queries, 3)
        for query in (invalid_json, missing_data, [{}]):
            with self.subTest(requery_type=type(query).__name__):
                mock = MockCommands(launches=['timeout'], target_lists=[[], [], query])
                self.assertEqual(run_case(mock)[0], 1)
                self.assert_counts(mock, 1, 1, 1)
                self.assertEqual(mock.target_queries, 3)

    def test_10_exact_workflow_structure(self):
        # Anchored top-level mapping, exact keys/values, no substring-only acceptance.
        block = re.search(r'(?m)^concurrency:\n((?:[ ]+[^\n]*\n)+)', TEXT)
        self.assertIsNotNone(block)
        pairs = {}
        for line in block[1].splitlines():
            match = re.fullmatch(r'  ([a-z-]+): ([^#]+)', line)
            self.assertIsNotNone(match)
            self.assertNotIn(match[1], pairs)
            pairs[match[1]] = match[2].strip()
        self.assertEqual(pairs, {'group': 'oracle-free-vm-hunt', 'cancel-in-progress': 'false'})
        self.assertEqual(len(re.findall(r'(?m)^concurrency:', TEXT)), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
