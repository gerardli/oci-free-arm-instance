"""Execute the production heredoc with real OCI SDK models/methods, fake I/O only."""
import builtins
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import oci
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / '.github/workflows/hunt-free-vm.yml').read_text()
SOURCE = '\n'.join(line[10:] for line in TEXT.split("          python3 - <<'PY'\n", 1)[1].rsplit('\n          PY', 1)[0].splitlines())
ARM, AMD = 'VM.Standard.A1.Flex', 'VM.Standard.E2.1.Micro'
REGION = 'ap-kulai-2'
TENANCY, TARGET = 'ocid1.tenancy.oc1..fake', 'ocid1.compartment.oc1..fake'
AD = 'fake:AP-KULAI-2-AD-1'
MARKER = 'FAKE_SENSITIVE_MUST_NOT_LEAVE_PROCESS'
M = oci.core.models


def response(data, headers=None):
    return oci.response.Response(200, headers or {}, data, None)


def instance(shape=ARM, state='PROVISIONING'):
    return M.Instance(id='ocid1.instance.oc1.ap-kulai-2.fake', shape=shape, lifecycle_state=state)


def image(shape=ARM, **kw):
    fields = dict(id='ocid1.image.oc1.ap-kulai-2.fake-' + ('arm' if shape == ARM else 'amd'),
                  compartment_id=None, operating_system='Canonical Ubuntu', operating_system_version='24.04',
                  lifecycle_state='AVAILABLE', size_in_mbs=1024,
                  display_name='Canonical-Ubuntu-24.04' + ('-aarch64' if shape == ARM else '') + '-2026.10.01-0')
    fields.update(kw)
    return M.Image(**fields)


def error(status=403, code='NotAuthorizedOrNotFound', message=MARKER):
    return oci.exceptions.ServiceError(status, code, {}, message)


def capacity():
    return error(500, 'InternalError', 'Out of host capacity.')


class FakeIO:
    def __init__(self, launches=(), targets=((),), overrides=None, notify_ok=True, disable_status=204):
        self.launches, self.targets = list(launches), list(targets)
        self.overrides = overrides or {}
        self.notify_ok, self.disable_status = notify_ok, disable_status
        self.queries, self.creates, self.notifications, self.disables, self.events, self.unexpected = [], [], [], [], [], []
        self.target_round = -1
        self.image_shapes = []

    def api(self, base, resource_path, method, **kw):
        try:
            return self._api(base, resource_path, method, **kw)
        except AssertionError as failure:
            trace = failure.__traceback__
            while trace.tb_next:
                trace = trace.tb_next
            self.unexpected.append('SDK contract violation at fixture line ' + str(trace.tb_lineno))
            raise

    def _api(self, base, resource_path, method, **kw):
        op = kw['operation_name']
        self.events.append(op)
        assert base.endpoint.endswith('oraclecloud.com/20160918') and REGION in base.endpoint
        assert base.timeout == (10, 30)
        assert os.environ['OCI_HEADER_PARSING_ERROR_MAX_RETRIES'] == '0'
        query = dict(kw.get('query_params') or [])
        compartment = query.get('compartmentId')
        page = query.get('page')
        page = page if isinstance(page, str) else None
        self.queries.append((op, compartment, page))
        if op == 'launch_instance':
            assert method == 'POST'
            body = kw['body']
            assert isinstance(body, M.LaunchInstanceDetails)
            assert body.compartment_id == TARGET and body.availability_domain == AD
            assert body.shape in (ARM, AMD)
            assert body.capacity_reservation_id == ''
            assert body.create_vnic_details.subnet_id == 'ocid1.subnet.oc1.ap-kulai-2.fake'
            assert body.create_vnic_details.assign_public_ip is True
            assert body.source_details.boot_volume_size_in_gbs == 50
            assert body.source_details.boot_volume_vpus_per_gb == 10
            assert body.source_details.image_id == image(body.shape).id
            if body.shape == ARM:
                assert body.shape_config.ocpus == 2 and body.shape_config.memory_in_gbs == 12
            else:
                assert body.shape_config is None
            assert kw['header_params']['opc-retry-token']
            assert os.environ['HUNT_DIAGNOSTIC_LOCK'] == 'false'
            assert os.environ.get('HUNT_DIAGNOSTIC_ONLY') != 'true'
            self.creates.append(body)
            result = self.launches.pop(0) if self.launches else instance(body.shape)
        else:
            assert method == 'GET', 'No unplanned writes'
            key = (op, compartment, page)
            if key in self.overrides or op in self.overrides:
                result = self.overrides.get(key, self.overrides.get(op))
                if callable(result):
                    result = result(query, kw)
            elif op == 'list_instances':
                if compartment == TARGET:
                    if page is None:
                        self.target_round += 1
                    result = self.targets[min(self.target_round, len(self.targets) - 1)]
                else:
                    result = []
            elif op == 'list_region_subscriptions':
                result = [oci.identity.models.RegionSubscription(region_name=REGION, is_home_region=True)]
            elif op == 'list_availability_domains':
                result = [oci.identity.models.AvailabilityDomain(name=AD)]
            elif op == 'get_subnet':
                result = M.Subnet(id='ocid1.subnet.oc1.ap-kulai-2.fake', lifecycle_state='AVAILABLE',
                                  prohibit_public_ip_on_vnic=False)
            elif op == 'list_compartments':
                assert query['compartmentIdInSubtree'] is True and query['accessLevel'] == 'ANY'
                result = [oci.identity.models.Compartment(id=TARGET, lifecycle_state='ACTIVE')]
            elif op in ('list_volumes', 'list_boot_volumes'):
                if op == 'list_boot_volumes':
                    assert query['availabilityDomain'] == AD
                result = []
            elif op == 'list_images':
                self.image_shapes.append(query['shape'])
                assert query['operatingSystem'] == 'Canonical Ubuntu'
                assert query['sortBy'] == 'TIMECREATED' and query['sortOrder'] == 'DESC'
                result = [image(query['shape'])]
            elif op == 'get_image':
                result = image(AMD if kw['path_params']['imageId'].endswith('amd') else ARM)
            elif op == 'get_image_shape_compatibility_entry':
                result = M.ImageShapeCompatibilityEntry(image_id=kw['path_params']['imageId'],
                                                        shape=kw['path_params']['shapeName'])
            else:
                raise AssertionError('Unexpected SDK operation')
        if isinstance(result, Exception):
            raise result
        if isinstance(result, oci.response.Response):
            return result
        return response(list(result) if isinstance(result, tuple) else result)

    def command(self, args, **kw):
        try:
            assert kw['capture_output'] is True and kw['text'] is True
            if args[1:4] == ['-m', 'pip', 'install']:
                assert args[-1] == 'oci==2.187.2'
                return subprocess.CompletedProcess(args, 0, '', '')
            assert args[:3] == ['curl', '--config', '-'], 'Unplanned external process'
            assert MARKER not in ' '.join(args)
            assert os.environ['HUNT_DIAGNOSTIC_LOCK'] == 'false'
            assert os.environ.get('HUNT_DIAGNOSTIC_ONLY') != 'true'
            config = kw['input']
            if '/disable' in config:
                self.disables.append(True)
                self.events.append('disable')
                assert args[args.index('--request') + 1] == 'PUT'
                assert '/actions/workflows/hunt-free-vm.yml/disable' in config
                return subprocess.CompletedProcess(args, 0, '\n' + str(self.disable_status), '')
            assert '/sendMessage' in config
            payload = next(line[7:] for line in config.splitlines() if line.startswith('data = '))
            message = json.loads(json.loads(payload))['text']
            assert MARKER not in message and 'ocid1.' not in message
            self.notifications.append(message)
            self.events.append('notify')
            return subprocess.CompletedProcess(args, 0, json.dumps({'ok': self.notify_ok}) + '\n200', '')
        except AssertionError:
            self.unexpected.append('external contract violation')
            raise


class Acceptance(unittest.TestCase):
    def run_case(self, fake=None, unmock_signer=False, allow_pre_api_failure=False, **extra):
        fake = fake or FakeIO()
        with tempfile.TemporaryDirectory(prefix='oci-sdk-test-') as home:
            env = dict(HOME=home, PATH=os.defpath, HUNT_DIAGNOSTIC_LOCK='false', HUNT_DIAGNOSTIC_ONLY='false',
                       OCI_CLI_TENANCY=TENANCY, OCI_CLI_USER='ocid1.user.oc1..fake',
                       OCI_CLI_FINGERPRINT=':'.join(['aa'] * 16), OCI_CLI_KEY_CONTENT=MARKER,
                       OCI_COMPARTMENT_ID=TARGET, OCI_SUBNET_ID='ocid1.subnet.oc1.ap-kulai-2.fake',
                       SSH_PUBLIC_KEY='ssh-ed25519 RkFLRQ==', TG_BOT_TOKEN=MARKER, TG_CHAT_ID='fake',
                       GITHUB_TOKEN=MARKER, GITHUB_REPOSITORY='fake/repo',
                       GITHUB_WORKFLOW_REF='fake/repo/.github/workflows/hunt-free-vm.yml@refs/heads/main')
            env.update(extra)
            env = {key: value for key, value in env.items() if value is not None}
            out, err = io.StringIO(), io.StringIO()
            old_logging = logging.root.manager.disable
            try:
                signer_patch = (patch('oci.signer.Signer', return_value=oci.signer.AbstractBaseSigner())
                                if not unmock_signer else contextlib.nullcontext())
                with patch.dict(os.environ, env, clear=True), \
                     patch('subprocess.run', side_effect=fake.command), \
                     signer_patch, \
                     patch('oci.base_client.BaseClient.call_api', autospec=True, side_effect=fake.api), \
                     patch('oci.config.from_file', side_effect=AssertionError('No config reads')), \
                     patch('oci._vendor.requests.sessions.Session.request', side_effect=AssertionError('No real HTTP')), \
                     patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')), \
                     patch('oci.retry.NoneRetryStrategy.make_retrying_call', autospec=True,
                           side_effect=lambda self, fn, *a, **k: fn(*a, **k)) as retry, \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        exec(compile(SOURCE, 'workflow-production', 'exec'), {'__name__': '__main__'})
                    except SystemExit as e:
                        status = e.code
                    else:
                        status = 0
                    # Credential-construction failures stop before any SDK call by design.
                    self.assertTrue(retry.called or unmock_signer or allow_pre_api_failure)
            finally:
                logging.disable(old_logging)
            self.assertEqual(fake.unexpected, [])
            self.assertNotIn(MARKER, out.getvalue() + err.getvalue())
            self.assertEqual(err.getvalue(), '')
            self.assertEqual(list(Path(home).iterdir()), [])
            return fake, status, out.getvalue()

    def stopped(self, fake, expected=1):
        f, status, output = self.run_case(fake)
        self.assertEqual(status, expected)
        self.assertEqual(len(f.disables), 1)
        self.assertTrue(f.notifications)
        self.assertLess(f.events.index('notify'), f.events.index('disable'))
        return f, output

    def test_01_workflow_contract(self):
        doc = yaml.load(TEXT, Loader=yaml.BaseLoader)
        self.assertEqual(doc['on']['schedule'], [{'cron': '7,22,37,52 * * * *'}])
        self.assertEqual(doc['permissions'], {'actions': 'write'})
        self.assertEqual(doc['concurrency'], {'group': 'oracle-free-vm-hunt', 'cancel-in-progress': 'false'})
        self.assertEqual(doc['jobs']['hunt']['env']['HUNT_DIAGNOSTIC_LOCK'], 'false')
        self.assertNotIn("['oci',", SOURCE)
        self.assertNotIn('tempfile', SOURCE)

    def test_02_existing_states(self):
        for shape in (ARM, AMD):
            for state in ('RUNNING', 'PROVISIONING', 'STARTING', 'STOPPING', 'STOPPED', 'MOVING', 'TERMINATING'):
                with self.subTest(shape=shape, state=state):
                    f, _ = self.stopped(FakeIO(targets=([instance(shape, state)],)), 0)
                    self.assertFalse(f.creates)
                    self.assertEqual(f.events[0], 'list_instances')

    def test_03_arm_success(self):
        f, _ = self.stopped(FakeIO(), 0)
        self.assertEqual([x.shape for x in f.creates], [ARM])
        self.assertEqual(f.image_shapes, [ARM])

    def test_04_arm_capacity_stops_cleanly_without_disabling(self):
        f, status, output = self.run_case(FakeIO(launches=[capacity()]))
        self.assertEqual(status, 0)
        self.assertIn('capacity_unavailable', output)
        self.assertEqual(len(f.creates), 1)
        self.assertFalse(f.disables or f.notifications)

    def test_05_both_capacity(self):
        f, status, output = self.run_case(FakeIO(launches=[capacity()]))
        self.assertEqual(status, 0)
        self.assertIn('capacity_unavailable', output)
        self.assertEqual(len(f.creates), 1)
        self.assertFalse(f.disables or f.notifications)

    def test_06_launch_429(self):
        f, status, _ = self.run_case(FakeIO(launches=[error(429)]))
        self.assertEqual(status, 0)
        self.assertEqual(len(f.creates), 1)
        self.assertFalse(f.disables or f.notifications)

    def test_07_auth_quota_configuration(self):
        for e in [error(401), error(403), error(400, 'LimitExceeded'), error(400, 'InvalidParameter')]:
            with self.subTest(status=e.status, code=e.code):
                f, _ = self.stopped(FakeIO(launches=[e]))
                self.assertEqual(len(f.creates), 1)

    def test_08_timeout_found(self):
        f, _ = self.stopped(FakeIO(launches=[oci.exceptions.RequestException(MARKER)],
                                 targets=[[], [], [instance()]]), 0)
        self.assertEqual(len(f.creates), 1)
        self.assertEqual(f.target_round, 2)

    def test_09_timeout_empty(self):
        f, output = self.stopped(FakeIO(launches=[oci.exceptions.RequestException(MARKER)]))
        self.assertEqual(len(f.creates), 1)
        self.assertEqual(f.target_round, 2)
        self.assertIn('unknown_result', output)

    def test_10_timeout_reconcile_429_alerts_and_disables(self):
        f, output = self.stopped(FakeIO(launches=[oci.exceptions.RequestException(MARKER)],
                                      targets=[[], [], error(429)]))
        self.assertEqual([x.shape for x in f.creates], [ARM])
        self.assertEqual(f.target_round, 2)  # initial, pre-launch, exactly one reconciliation
        self.assertEqual(len(f.notifications), 1)
        self.assertIn('Launch outcome unknown', f.notifications[0])
        self.assertIn('category=rate_limit http=429', output)

    def test_11_malformed_launch(self):
        f, _ = self.stopped(FakeIO(launches=[None]))
        self.assertEqual(len(f.creates), 1)
        self.assertEqual(f.target_round, 2)

    def test_12_query_failures(self):
        for op in ['list_instances', 'list_region_subscriptions', 'list_availability_domains',
                   'get_subnet', 'list_compartments', 'list_volumes', 'list_boot_volumes', 'list_images']:
            with self.subTest(op=op):
                f, _ = self.stopped(FakeIO(overrides={op: error(403)}))
                self.assertFalse(f.creates)

    def test_13_second_instance_page(self):
        overrides = {('list_instances', TARGET, None): response([], {'opc-next-page': 'next'}),
                     ('list_instances', TARGET, 'next'): [instance()]}
        f, _ = self.stopped(FakeIO(overrides=overrides), 0)
        self.assertFalse(f.creates)
        self.assertIn(('list_instances', TARGET, 'next'), f.queries)

    def test_14_second_page_failure(self):
        for op, compartment in [('list_instances', TARGET), ('list_compartments', TENANCY),
                                ('list_volumes', TENANCY), ('list_boot_volumes', TARGET), ('list_images', TARGET)]:
            with self.subTest(op=op):
                f, _ = self.stopped(FakeIO(overrides={(op, compartment, None): response([], {'opc-next-page': 'next'}),
                                                     (op, compartment, 'next'): error(403)}))
                self.assertFalse(f.creates)

    def test_15_repeated_page_and_bad_collection(self):
        for result in [response([], {'opc-next-page': 'same'}), response(None), response({'data': []})]:
            with self.subTest(kind=type(result.data).__name__):
                f, _ = self.stopped(FakeIO(overrides={'list_instances': result}))
                self.assertFalse(f.creates)

    def test_16_other_compartment_and_root(self):
        for c in [TENANCY, 'ocid1.compartment.oc1..other']:
            with self.subTest(compartment=c):
                overrides = {'list_compartments': [oci.identity.models.Compartment(id=TARGET, lifecycle_state='ACTIVE'),
                                                  oci.identity.models.Compartment(id=c, lifecycle_state='ACTIVE')],
                             ('list_instances', c, None): [instance()]}
                f, _ = self.stopped(FakeIO(overrides=overrides))
                self.assertFalse(f.creates)

    def test_17_total_storage(self):
        for size, allowed in [(150, True), (151, False)]:
            with self.subTest(size=size):
                f, _ = self.stopped(FakeIO(overrides={('list_volumes', TENANCY, None):
                    [M.Volume(id='fake-volume', lifecycle_state='AVAILABLE', size_in_gbs=size)]}), 0 if allowed else 1)
                self.assertEqual(bool(f.creates), allowed)

    def test_18_arm_capacity_stops_cleanly(self):
        f, status, output = self.run_case(FakeIO(launches=[capacity()]))
        self.assertEqual(status, 0)
        self.assertIn('ARM rejected', output)
        self.assertFalse(f.disables or f.notifications)

    def test_19_invalid_image_architecture(self):
        for bad in [image(AMD), image(ARM, operating_system_version='22.04'), image(ARM, size_in_mbs=60000),
                    image(ARM, compartment_id=TARGET)]:
            with self.subTest(image=bad.display_name):
                f, _ = self.stopped(FakeIO(overrides={'list_images': [bad]}))
                self.assertFalse(f.creates)

    def test_20_image_override(self):
        f, status, _ = self.run_case(IMAGE_ID_ARM=image().id)
        self.assertEqual(status, 0)
        self.assertIn('get_image_shape_compatibility_entry', f.events)
        self.assertEqual(len(f.creates), 1)

    def test_21_notification_failure_still_disables(self):
        f, _ = self.stopped(FakeIO(notify_ok=False))
        self.assertEqual(len(f.creates), 1)

    def test_22_disable_failure_alerts(self):
        for code in (200, 403):
            with self.subTest(status=code):
                f, output = self.stopped(FakeIO(disable_status=code))
                self.assertEqual(len(f.notifications), 2)
                self.assertIn('manually disable', output)

    def test_23_diagnostic_locks(self):
        for lock, mode in [('true', 'false'), ('', 'false'), ('FALSE', 'false'), (None, 'false'), ('false', 'true')]:
            with self.subTest(lock=lock, mode=mode):
                f, status, _ = self.run_case(HUNT_DIAGNOSTIC_LOCK=lock, HUNT_DIAGNOSTIC_ONLY=mode)
                self.assertEqual(status, 0)
                self.assertFalse(f.creates or f.notifications or f.disables)

    def test_24_ambiguous_500_is_not_capacity(self):
        f, _ = self.stopped(FakeIO(launches=[error(500, 'InternalError', 'temporary unavailable')]))
        self.assertEqual(len(f.creates), 1)

    def test_25_unknown_state(self):
        f, _ = self.stopped(FakeIO(targets=[[instance(state='UNKNOWN_ENUM_VALUE')]]))
        self.assertFalse(f.creates)

    def test_26_home_and_subnet(self):
        for overrides in [
            {'list_region_subscriptions': [oci.identity.models.RegionSubscription(region_name='other', is_home_region=True)]},
            {'get_subnet': M.Subnet(id='ocid1.subnet.oc1.ap-kulai-2.fake', lifecycle_state='AVAILABLE', prohibit_public_ip_on_vnic=True)}]:
            with self.subTest(case=list(overrides)[0]):
                f, _ = self.stopped(FakeIO(overrides=overrides))
                self.assertFalse(f.creates)

    def test_27_sdk_header_retry_disabled(self):
        # Exercise the real SDK request layer too: NoneRetryStrategy alone does not disable this loop.
        from urllib3.exceptions import HeaderParsingError
        config = dict(region=REGION, tenancy=TENANCY, user='ocid1.user.oc1..fake',
                      fingerprint=':'.join(['aa'] * 16), key_content=MARKER)
        production, status, _ = self.run_case()
        self.assertEqual(status, 0)
        client = oci.core.ComputeClient(config, signer=oci.signer.AbstractBaseSigner(),
                                       retry_strategy=oci.retry.NoneRetryStrategy())
        old = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            with patch.dict(os.environ, {'OCI_HEADER_PARSING_ERROR_MAX_RETRIES': '0'}), \
                 patch.object(client.base_client.session, 'request', side_effect=HeaderParsingError([], MARKER)) as request, \
                 patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')):
                with self.assertRaises(oci.exceptions.RequestException):
                    client.launch_instance(production.creates[0], retry_strategy=oci.retry.NoneRetryStrategy())
                self.assertEqual(request.call_count, 1)
                wire = json.loads(request.call_args.kwargs['data'])
                self.assertEqual(wire['sourceDetails']['bootVolumeSizeInGBs'], 50)
                self.assertEqual(wire['sourceDetails']['bootVolumeVpusPerGB'], 10)
                self.assertEqual(wire['shapeConfig'], {'ocpus': 2, 'memoryInGBs': 12})
                self.assertTrue(wire['createVnicDetails']['assignPublicIp'])
        finally:
            logging.disable(old)

    def test_28_ordinary_query_429_is_silent_round_stop(self):
        for op in ['list_instances', 'list_region_subscriptions', 'list_availability_domains',
                   'get_subnet', 'list_compartments', 'list_volumes', 'list_boot_volumes', 'list_images']:
            with self.subTest(op=op):
                f, status, output = self.run_case(FakeIO(overrides={op: error(429)}))
                self.assertEqual(status, 0)
                self.assertEqual(output, '')
                self.assertFalse(f.creates or f.notifications or f.disables)

    def test_29_real_private_key_loading_is_classified(self):
        # The signer must not be mocked here: exercise the real SDK key-loading path.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        plain = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode()
        locked = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.BestAvailableEncryption(b'test passphrase')).decode()
        public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        cases = [('private_key_passphrase', locked),
                 ('private_key_is_public', public),
                 ('private_key_invalid', '-----BEGIN PRIVATE KEY-----\\n' + 'A' * 40 + '\\n-----END PRIVATE KEY-----\\n'),
                 # Two characters and one character produce plural/singular SDK wording; both must classify.
                 ('private_key_non_ascii', plain.replace('BEGIN', '中文 BEGIN', 1)),
                 ('private_key_non_ascii', plain.replace('BEGIN', '中 BEGIN', 1))]
        for category, content in cases:
            with self.subTest(category=category):
                f, status, output = self.run_case(OCI_CLI_KEY_CONTENT=content, unmock_signer=True)
                self.assertEqual(status, 1)
                self.assertIn('category=' + category, output)
                self.assertIn('stage=credentials', output)
                self.assertFalse(f.creates)
                self.assertEqual(len(f.notifications), 1)  # fail-closed: alert once, disable once
                self.assertEqual(len(f.disables), 1)
                self.assertNotIn('BEGIN', output)
                self.assertNotIn('test passphrase', output)
                self.assertIn('category=' + category, f.notifications[0])
                self.assertNotIn(content, f.notifications[0])
            with self.subTest(category=category, locked=True):
                # Under the diagnostic lock the same defect stays read-only: no launch, notify or disable.
                f, status, output = self.run_case(OCI_CLI_KEY_CONTENT=content, unmock_signer=True,
                                                  HUNT_DIAGNOSTIC_LOCK='true')
                self.assertEqual(status, 1)
                self.assertIn('category=' + category, output)
                self.assertFalse(f.creates or f.notifications or f.disables)
        # A genuine unencrypted key must load and let the run proceed to the read-only stop.
        f, status, output = self.run_case(OCI_CLI_KEY_CONTENT=plain, unmock_signer=True,
                                          HUNT_DIAGNOSTIC_LOCK='true')
        self.assertEqual(status, 0)
        self.assertIn('checks_passed', output)

    def test_30_credential_field_shape_is_classified(self):
        # Client construction validates fingerprint/user/tenancy shape; each field gets its own label.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        plain = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode()
        good_fp = ':'.join(['ab'] * 16)
        cases = {'credential_field_fingerprint': {'OCI_CLI_FINGERPRINT': 'aa:bb:cc'},
                 'credential_field_user': {'OCI_CLI_USER': 'not-an-ocid@example.com'},
                 'credential_field_tenancy': {'OCI_CLI_TENANCY': 'my-tenancy'}}
        for category, override in cases.items():
            with self.subTest(category=category):
                f, status, output = self.run_case(OCI_CLI_KEY_CONTENT=plain, unmock_signer=True,
                                                  HUNT_DIAGNOSTIC_LOCK='true', **override)
                self.assertEqual(status, 1)
                self.assertIn('category=' + category, output)
                self.assertIn('stage=credentials', output)
                self.assertFalse(f.creates or f.notifications or f.disables)
                self.assertNotIn(override[list(override)[0]], output)
        # A well-formed credential set must still reach the read-only stop.
        f, status, output = self.run_case(OCI_CLI_KEY_CONTENT=plain, unmock_signer=True,
                                          HUNT_DIAGNOSTIC_LOCK='true', OCI_CLI_FINGERPRINT=good_fp)
        self.assertEqual(status, 0)
        self.assertIn('checks_passed', output)
        # Outside the lock the same defect stays fail-closed: one alert, one disable, no creation.
        f, status, output = self.run_case(OCI_CLI_KEY_CONTENT=plain, unmock_signer=True,
                                          OCI_CLI_FINGERPRINT='aa:bb:cc')
        self.assertEqual(status, 1)
        self.assertIn('category=credential_field_fingerprint', output)
        self.assertEqual(len(f.notifications), 1)
        self.assertEqual(len(f.disables), 1)
        self.assertFalse(f.creates)
        self.assertNotIn('aa:bb:cc', f.notifications[0])
        self.assertIn('credential_field_fingerprint', f.notifications[0])


    def test_31_unexpected_client_setup_is_safely_staged(self):
        with patch('oci.identity.IdentityClient', side_effect=RuntimeError(MARKER)):
            f, status, output = self.run_case(
                HUNT_DIAGNOSTIC_LOCK='true', allow_pre_api_failure=True)
        self.assertEqual(status, 1)
        self.assertIn(
            'stage=credentials credential_step=identity_client category=runner_error http=none',
            output)
        self.assertNotIn(MARKER, output)
        self.assertFalse(f.creates or f.notifications or f.disables)

    def test_32_sdk_import_failures_have_safe_fixed_categories(self):
        cases = (
            (ModuleNotFoundError(MARKER, name="oci"), "sdk_module_missing"),
            (ModuleNotFoundError(MARKER, name="opaque_dependency"), "sdk_dependency_missing"),
            (OSError(MARKER), "sdk_os_error"),
            (ImportError(MARKER), "sdk_import_error"),
            (RuntimeError(MARKER), "sdk_import_exception"),
        )
        for error, category in cases:
            with self.subTest(category=category):
                fake = FakeIO()
                sdk_ready = False
                original_command = fake.command

                def command_then_arm_import_failure(args, **kwargs):
                    nonlocal sdk_ready
                    result = original_command(args, **kwargs)
                    if args[1:4] == ["-m", "pip", "install"]:
                        sdk_ready = True
                    return result

                fake.command = command_then_arm_import_failure
                original_import = builtins.__import__

                def fail_sdk_import(name, *args, **kwargs):
                    if name == "oci" and sdk_ready:
                        raise error
                    return original_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=fail_sdk_import):
                    fake, status, output = self.run_case(
                        fake=fake, HUNT_DIAGNOSTIC_LOCK='true', allow_pre_api_failure=True)
                self.assertEqual(status, 1)
                self.assertIn(
                    f"stage=credentials credential_step=sdk_import category={category} http=none",
                    output)
                self.assertNotIn(MARKER, output)
                self.assertNotIn("opaque_dependency", output)
                self.assertFalse(fake.creates or fake.notifications or fake.disables)


if __name__ == '__main__':
    assert oci.__version__ == '2.187.2', 'Use the pinned SDK for contract verification'
    unittest.main(verbosity=2)
