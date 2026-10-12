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
        self.blank_list_responses = 0
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
            outcome = self.overrides.get('install_outcome')
            if isinstance(outcome, Exception):
                raise outcome
            return outcome or subprocess.CompletedProcess(command, 0, '', '')
        if command[0] == 'curl':
            assert os.environ.get('HUNT_DIAGNOSTIC_LOCK', 'true') == 'false' and os.environ.get('HUNT_DIAGNOSTIC_ONLY') != 'true', 'Diagnostic mode must not notify or disable'
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
        if operation == self.overrides.get('operation_failure'):
            outcome = self.overrides['operation_outcome']
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
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
            assert os.environ.get('HUNT_DIAGNOSTIC_LOCK', 'true') == 'false' and os.environ.get('HUNT_DIAGNOSTIC_ONLY') != 'true', 'Diagnostic mode must never launch'
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
        # oci-cli 3.94.2 render() emits no JSON when data is falsy and no display header remains.
        if isinstance(data, list) and not data:
            self.blank_list_responses += 1
            rendered = ''
        else:
            rendered = json.dumps({'data': data})
        return subprocess.CompletedProcess(command, 0, rendered, '')

    @staticmethod
    def assert_capture(kwargs):
        assert kwargs.get('capture_output') is True
        assert kwargs.get('text') is True
        assert kwargs.get('timeout') in (25, 60, 90, 180)


def run_case(mock, environment=None):
    env = {
        'HUNT_DIAGNOSTIC_LOCK': 'false',  # Explicitly test the preserved normal path after lock release.
        'OCI_CLI_TENANCY': TENANCY, 'OCI_CLI_USER': 'mock-user', 'OCI_CLI_FINGERPRINT': 'mock-fingerprint',
        'OCI_CLI_KEY_CONTENT': SENSITIVE, 'OCI_COMPARTMENT_ID': COMPARTMENT,
        'OCI_SUBNET_ID': 'ocid1.subnet.oc1.ap-kulai-2.mock', 'SSH_PUBLIC_KEY': 'ssh-ed25519 bW9jaw==',
        'TG_BOT_TOKEN': SENSITIVE, 'TG_CHAT_ID': 'mock-chat', 'GITHUB_TOKEN': SENSITIVE,
        'GITHUB_REPOSITORY': 'mock-owner/mock-repo',
        'GITHUB_WORKFLOW_REF': 'mock-owner/mock-repo/.github/workflows/hunt-free-vm.yml@refs/heads/main',
    }
    env.update(environment or {})
    env = {key: value for key, value in env.items() if value is not None}
    output = io.StringIO()
    stderr = io.StringIO()
    with tempfile.TemporaryDirectory(prefix='oci-offline-') as home:
        env.update({'HOME': home, 'TMPDIR': home, 'OCI_CLI_CONFIG_FILE': str(Path(home) / 'absent-config')})
        # Force tempfile isolation as well as credential/environment isolation.
        with patch.object(tempfile, 'tempdir', home), patch.dict(os.environ, env, clear=True), \
                patch('subprocess.run', side_effect=mock), contextlib.redirect_stdout(output), contextlib.redirect_stderr(stderr):
            try:
                exec(compile(SOURCE, str(WORKFLOW), 'exec'), {'__name__': '__main__'})
                status = 0
            except SystemExit as exit_result:
                status = exit_result.code
        assert not mock.unexpected, 'Unknown external call was blocked'
        assert SENSITIVE not in output.getvalue() + stderr.getvalue()
        assert 'ocid1.' not in output.getvalue() + stderr.getvalue()
        assert all(not path.exists() for path in mock.secret_files), 'Temporary credentials were not removed'
    return status, output.getvalue() + stderr.getvalue()


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


    def test_11_diagnostic_mode_never_mutates(self):
        mode = {'HUNT_DIAGNOSTIC_ONLY': 'true'}
        for targets in ([], [instance()], [instance(AMD, 'TERMINATING')]):
            with self.subTest(existing=bool(targets)):
                mock = MockCommands(target_lists=[targets])
                status, output = run_case(mock, mode)
                self.assertEqual(status, 0)
                self.assert_counts(mock, 0, 0, 0)
                self.assertEqual(mock.target_queries, 1)
                self.assertEqual(mock.events.count('image:' + ARM), 1)
                self.assertEqual(mock.events.count('image:' + AMD), 1)
                self.assertIn('Diagnostic-only complete', output)
        for query in (service_error(429, 'TooManyRequests', SENSITIVE),
                      service_error(401, 'NotAuthenticated', SENSITIVE),
                      OSError(SENSITIVE), [{}]):
            with self.subTest(query_type=type(query).__name__):
                mock = MockCommands(target_lists=[query])
                status, output = run_case(mock, mode)
                self.assertEqual(status, 1)
                self.assert_counts(mock, 0, 0, 0)
                self.assertIn('stage=target_instances', output)
        mock = MockCommands(overrides={'home': 'invalid-region'})
        self.assertEqual(run_case(mock, mode)[0], 1)
        self.assert_counts(mock, 0, 0, 0)
        # Independent launch guard, even if a future caller bypasses the main-mode branch.
        namespace = {}
        prefix = SOURCE.split("\ntry:\n    need(env.get('HUNT_DIAGNOSTIC_ONLY'", 1)[0]
        with patch.dict(os.environ, mode, clear=True), patch('subprocess.run') as external:
            exec(compile(prefix, str(WORKFLOW), 'exec'), namespace)
            with self.assertRaises(namespace['Halt']):
                namespace['oci']('compute', 'instance', 'launch', '--shape', ARM, launch=True)
            external.assert_not_called()

    def test_12_fixed_stage_and_error_categories(self):
        stages = {
            ('iam', 'region-subscription', 'list'): 'region_subscription',
            ('iam', 'availability-domain', 'list'): 'availability_domains',
            ('network', 'subnet', 'get'): 'subnet',
            ('iam', 'compartment', 'list'): 'compartments',
            ('bv', 'volume', 'list'): 'block_volumes',
            ('bv', 'boot-volume', 'list'): 'boot_volumes',
            ('compute', 'instance', 'list'): 'other_instances',
        }
        for operation, stage_name in stages.items():
            for mode in ('true', 'false'):
                with self.subTest(stage=stage_name, diagnostic=mode):
                    mock = MockCommands(overrides={'operation_failure': operation,
                        'operation_outcome': service_error(403, SENSITIVE, SENSITIVE)})
                    status, output = run_case(mock, {'HUNT_DIAGNOSTIC_ONLY': mode})
                    self.assertEqual(status, 1)
                    self.assert_counts(mock, 0, 0 if mode == 'true' else 1, 0 if mode == 'true' else 1)
                    self.assertIn('stage=' + stage_name + ' category=authorization http=403', output)
        outcomes = [(service_error(401, 'NotAuthenticated', SENSITIVE), 'authentication', '401'),
                    (service_error(429, 'TooManyRequests', SENSITIVE), 'rate_limit', '429'),
                    (service_error(400, 'LimitExceeded', SENSITIVE), 'quota', '400'),
                    (service_error(500, SENSITIVE, SENSITIVE), 'service_error', '500'),
                    (service_error(None, SENSITIVE, SENSITIVE), 'cli_error', 'none'),
                    (subprocess.TimeoutExpired(['mock'], 60, output=SENSITIVE, stderr=SENSITIVE), 'timeout', 'none'),
                    (OSError(SENSITIVE), 'transport', 'none'),
                    (subprocess.CompletedProcess([], 0, SENSITIVE, SENSITIVE), 'invalid_response', 'none')]
        for outcome, category, http_status in outcomes:
            with self.subTest(category=category):
                mock = MockCommands(overrides={'operation_failure': ('iam', 'region-subscription', 'list'),
                                              'operation_outcome': outcome})
                status, output = run_case(mock, {'HUNT_DIAGNOSTIC_ONLY': 'true'})
                self.assertEqual(status, 1)
                self.assert_counts(mock, 0, 0, 0)
                self.assertIn('stage=region_subscription category=' + category + ' http=' + http_status, output)
        for shape in (ARM, AMD):
            for mode in ('true', 'false'):
                mock = MockCommands(overrides={'image_failure': shape})
                status, output = run_case(mock, {'HUNT_DIAGNOSTIC_ONLY': mode})
                self.assertEqual(status, 1)
                self.assert_counts(mock, 0, 0 if mode == 'true' else 1, 0 if mode == 'true' else 1)
                self.assertIn('stage=image_' + ('arm' if shape == ARM else 'amd') + ' category=authentication http=401', output)
            mock = MockCommands(overrides={'operation_failure': ('compute', 'image-shape-compatibility-entry', 'get'),
                                          'operation_outcome': service_error(403, SENSITIVE, SENSITIVE)})
            status, output = run_case(mock, {'HUNT_DIAGNOSTIC_ONLY': 'true',
                'IMAGE_ID_ARM' if shape == ARM else 'IMAGE_ID_AMD': IMAGES[shape]})
            self.assertEqual(status, 1)
            self.assert_counts(mock, 0, 0, 0)
            self.assertIn('stage=image_compat_' + ('arm' if shape == ARM else 'amd'), output)
        for outcome, category in ((OSError(SENSITIVE), 'transport'),
                                  (subprocess.TimeoutExpired(['mock'], 180, stderr=SENSITIVE), 'timeout'),
                                  (subprocess.CompletedProcess([], 1, SENSITIVE, SENSITIVE), 'validation_error')):
            mock = MockCommands(overrides={'install_outcome': outcome})
            status, output = run_case(mock, {'HUNT_DIAGNOSTIC_ONLY': 'true'})
            self.assertEqual(status, 1)
            self.assert_counts(mock, 0, 0, 0)
            self.assertIn('stage=cli_install category=' + category, output)

    def test_13_untrusted_errors_never_relayed(self):
        for status in (SENSITIVE, True, {'untrusted': SENSITIVE}, 999):
            for mode in ('true', 'false'):
                mock = MockCommands(overrides={'operation_failure': ('iam', 'region-subscription', 'list'),
                    'operation_outcome': service_error(status, SENSITIVE, SENSITIVE)})
                result, output = run_case(mock, {'HUNT_DIAGNOSTIC_ONLY': mode})
                self.assertEqual(result, 1)
                self.assertIn('category=cli_error http=none', output)
                self.assert_counts(mock, 0, 0 if mode == 'true' else 1, 0 if mode == 'true' else 1)
        for payload in (SENSITIVE, 'ServiceError: []', 'ServiceError: null',
                        'ServiceError: {"status":401,"code":[],"message":{}}'):
            mock = MockCommands(overrides={'operation_failure': ('iam', 'region-subscription', 'list'),
                'operation_outcome': subprocess.CompletedProcess([], 1, payload, payload)})
            status, output = run_case(mock, {'HUNT_DIAGNOSTIC_ONLY': 'true'})
            self.assertEqual(status, 1)
            self.assert_counts(mock, 0, 0, 0)
            self.assertNotIn(payload, output)

    def test_14_diagnostic_selector_structure(self):
        self.assertRegex(TEXT, r'workflow_dispatch:\n    inputs:\n      diagnostic_only:\n        description: [^\n]+\n        type: boolean\n        default: false')
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.diagnostic_only == true && 'true' || 'false'", TEXT)
        self.assertNotIn('${{ inputs.diagnostic_only }}', SOURCE)
        self.assertIn("HUNT_DIAGNOSTIC_LOCK: 'true'", TEXT)
        for event in ('schedule', 'workflow_dispatch'):
            for lock in ('true', '', 'unexpected', None):
                mock = MockCommands()
                status, output = run_case(mock, {'GITHUB_EVENT_NAME': event, 'HUNT_DIAGNOSTIC_LOCK': lock, 'HUNT_DIAGNOSTIC_ONLY': 'false'})
                self.assertEqual(status, 0)
                self.assert_counts(mock, 0, 0, 0)
                self.assertIn('Diagnostic-only complete:', output)
        for environment in ({}, {'HUNT_DIAGNOSTIC_ONLY': 'false'}):
            mock = MockCommands(launches=[instance()])
            self.assertEqual(run_case(mock, environment)[0], 0)
            self.assert_counts(mock, 1, 1, 1)


    def test_15_cli_empty_collection_stdout(self):
        # Known list --all with exit 0 and blank stdout/stderr is an empty collection, not a query failure.
        mock = MockCommands(launches=[instance()])
        self.assertEqual(run_case(mock)[0], 0)
        self.assertGreater(mock.blank_list_responses, 0)
        self.assert_counts(mock, 1, 1, 1)

        for stdout, stderr in (('{"data":[]}', ''), ('\n', '\n')):
            with self.subTest(stdout=stdout, stderr=stderr):
                rendered = subprocess.CompletedProcess([], 0, stdout, stderr)
                mock = MockCommands(target_lists=[rendered], launches=[instance()])
                self.assertEqual(run_case(mock)[0], 0)
                self.assert_counts(mock, 1, 1, 1)

        noisy = subprocess.CompletedProcess([], 0, '', 'Warning: ' + SENSITIVE)
        mock = MockCommands(target_lists=[noisy], launches=[instance()])
        status, output = run_case(mock)
        self.assertEqual(status, 1)
        self.assert_counts(mock, 0, 1, 1)
        self.assertIn('stage=target_instances category=stdout_empty http=none', output)

        header_only = subprocess.CompletedProcess([], 0, '{"opc-total-items":"0"}', '')
        mock = MockCommands(target_lists=[header_only], launches=[instance()])
        status, output = run_case(mock)
        self.assertEqual(status, 1)
        self.assert_counts(mock, 0, 1, 1)
        self.assertIn('stage=target_instances category=invalid_response http=none', output)

        mock = MockCommands(launches=[subprocess.CompletedProcess([], 0, '', ''), instance()])
        status, output = run_case(mock)
        self.assertEqual(status, 1)
        self.assert_counts(mock, 1, 1, 1)
        self.assertEqual(mock.target_queries, 3)
        self.assertEqual(mock.launches, [instance()])
        self.assertIn('Launch outcome unknown', mock.notifications[0])
        self.assertIn('stage=target_instances category=unknown_result http=none', output)
        # Direct parser check: requery replaces the global stage, so the launch category is asserted here.
        namespace = {}
        prefix = SOURCE.split("\ntry:\n    need(env.get('HUNT_DIAGNOSTIC_ONLY'", 1)[0]
        def blank_success(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch.dict(os.environ, {'HUNT_DIAGNOSTIC_LOCK': 'false', 'OCI_COMPARTMENT_ID': COMPARTMENT}, clear=True), \
                patch('subprocess.run', side_effect=blank_success):
            exec(compile(prefix, str(WORKFLOW), 'exec'), namespace)
            self.assertEqual(namespace['oci']('bv', 'volume', 'list', '--compartment-id', COMPARTMENT, '--all'), [])
            with self.assertRaises(namespace['OciError']) as caught:
                namespace['oci']('compute', 'instance', 'list', '--compartment-id', COMPARTMENT)
            self.assertEqual(caught.exception.category, 'stdout_empty')
            with self.assertRaises(namespace['OciError']) as caught:
                namespace['oci']('compute', 'instance', 'launch', '--shape', ARM, launch=True)
            self.assertEqual(caught.exception.category, 'stdout_empty')
            with self.assertRaises(namespace['OciError']) as caught:
                namespace['oci']('network', 'subnet', 'get', '--subnet-id', 'mock')
            self.assertEqual(caught.exception.category, 'stdout_empty')

        mock = MockCommands(overrides={
            'operation_failure': ('compute', 'image', 'get'),
            'operation_outcome': subprocess.CompletedProcess([], 0, '', ''),
        })
        status, output = run_case(mock, {'IMAGE_ID_ARM': IMAGES[ARM], 'IMAGE_ID_AMD': IMAGES[AMD]})
        self.assertEqual(status, 1)
        self.assert_counts(mock, 0, 1, 1)
        self.assertEqual(mock.target_queries, 0)
        self.assertIn('stage=image_arm category=stdout_empty http=none', output)

        mock = MockCommands()
        status, output = run_case(mock, {'HUNT_DIAGNOSTIC_LOCK': 'true'})
        self.assertEqual(status, 0)
        self.assertGreater(mock.blank_list_responses, 0)
        self.assert_counts(mock, 0, 0, 0)
        self.assertIn('Diagnostic-only complete', output)


if __name__ == '__main__':
    unittest.main(verbosity=2)
