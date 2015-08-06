# -*- coding: utf-8 -*-
# Copyright 2009-2015 Yelp and Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for EMRJobRunner"""
import copy
import getpass
import os
import os.path
import posixpath
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from io import BytesIO

import mrjob
import mrjob.emr
from mrjob.emr import EMRJobRunner
from mrjob.emr import _DEFAULT_AMI_VERSION
from mrjob.emr import _MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH
from mrjob.emr import _lock_acquire_step_1
from mrjob.emr import _lock_acquire_step_2
from mrjob.emr import _yield_all_bootstrap_actions
from mrjob.emr import _yield_all_clusters
from mrjob.emr import _yield_all_instance_groups
from mrjob.emr import _yield_all_steps
from mrjob.emr import attempt_to_acquire_lock
from mrjob.emr import filechunkio
from mrjob.fs.s3 import S3Filesystem
from mrjob.job import MRJob
from mrjob.parse import JOB_KEY_RE
from mrjob.parse import parse_s3_uri
from mrjob.pool import _pool_hash_and_name
from mrjob.py2 import PY2
from mrjob.py2 import StringIO
from mrjob.ssh import SSH_LOG_ROOT
from mrjob.ssh import SSH_PREFIX
from mrjob.util import bash_wrap
from mrjob.util import log_to_stream
from mrjob.util import tar_and_gzip

from tests.mockboto import MockBotoTestCase
from tests.mockboto import MockEmrConnection
from tests.mockboto import MockEmrObject
from tests.mockssh import mock_ssh_dir
from tests.mockssh import mock_ssh_file
from tests.mr_hadoop_format_job import MRHadoopFormatJob
from tests.mr_jar_and_streaming import MRJarAndStreaming
from tests.mr_just_a_jar import MRJustAJar
from tests.mr_two_step_job import MRTwoStepJob
from tests.mr_word_count import MRWordCount
from tests.py2 import Mock
from tests.py2 import TestCase
from tests.py2 import mock
from tests.py2 import patch
from tests.py2 import skipIf
from tests.quiet import logger_disabled
from tests.quiet import no_handlers_for_logger
from tests.sandbox import mrjob_conf_patcher
from tests.sandbox import patch_fs_s3

try:
    import boto
    import boto.emr
    import boto.emr.connection
    import boto.exception
    boto  # quiet "redefinition of unused ..." warning from pyflakes
except ImportError:
    boto = None

# used to match command lines
if PY2:
    PYTHON_BIN = 'python'
else:
    PYTHON_BIN = 'python3'


class EMRJobRunnerEndToEndTestCase(MockBotoTestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'emr': {
        'check_emr_status_every': 0.00,
        's3_sync_wait_time': 0.00,
        'additional_emr_info': {'key': 'value'}
    }}}

    def test_end_to_end(self):
        # read from STDIN, a local file, and a remote file
        stdin = BytesIO(b'foo\nbar\n')

        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'wb') as local_input_file:
            local_input_file.write(b'bar\nqux\n')

        remote_input_path = 's3://walrus/data/foo'
        self.add_mock_s3_data({'walrus': {'data/foo': b'foo\n'}})

        # setup fake output
        self.mock_emr_output = {('j-MOCKCLUSTER0', 1): [
            b'1\t"qux"\n2\t"bar"\n', b'2\t"foo"\n5\tnull\n']}

        mr_job = MRHadoopFormatJob(['-r', 'emr', '-v',
                                    '-', local_input_path, remote_input_path,
                                    '--jobconf', 'x=y'])
        mr_job.sandbox(stdin=stdin)

        local_tmp_dir = None
        results = []

        mock_s3_fs_snapshot = copy.deepcopy(self.mock_s3_fs)

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, EMRJobRunner)

            # make sure that initializing the runner doesn't affect S3
            # (Issue #50)
            self.assertEqual(mock_s3_fs_snapshot, self.mock_s3_fs)

            # make sure AdditionalInfo was JSON-ified from the config file.
            # checked now because you can't actually read it from the job flow
            # on real EMR.
            self.assertEqual(runner._opts['additional_emr_info'],
                             '{"key": "value"}')
            runner.run()

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

            local_tmp_dir = runner._get_local_tmp_dir()
            # make sure cleanup hasn't happened yet
            self.assertTrue(os.path.exists(local_tmp_dir))
            self.assertTrue(any(runner.ls(runner.get_output_dir())))

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.status.state, 'TERMINATED')
            name_match = JOB_KEY_RE.match(cluster.name)
            self.assertEqual(name_match.group(1), 'mr_hadoop_format_job')
            self.assertEqual(name_match.group(2), getpass.getuser())

            # make sure our input and output formats are attached to
            # the correct steps
            emr_conn = runner.make_emr_conn()
            steps = list(_yield_all_steps(emr_conn, runner.get_cluster_id()))

            step_0_args = [a.value for a in steps[0].config.args]
            step_1_args = [a.value for a in steps[1].config.args]

            self.assertIn('-inputformat', step_0_args)
            self.assertNotIn('-outputformat', step_0_args)
            self.assertNotIn('-inputformat', step_1_args)
            self.assertIn('-outputformat', step_1_args)

            # make sure jobconf got through
            self.assertIn('-D', step_0_args)
            self.assertIn('x=y', step_0_args)
            self.assertIn('-D', step_1_args)
            # job overrides jobconf in step 1
            self.assertIn('x=z', step_1_args)

            # make sure mrjob.tar.gz is created and uploaded as
            # a bootstrap file
            self.assertTrue(os.path.exists(runner._mrjob_tar_gz_path))
            self.assertIn(runner._mrjob_tar_gz_path,
                          runner._upload_mgr.path_to_uri())
            self.assertIn(runner._mrjob_tar_gz_path,
                          runner._bootstrap_dir_mgr.paths())

        self.assertEqual(sorted(results),
                         [(1, 'qux'), (2, 'bar'), (2, 'foo'), (5, None)])

        # make sure cleanup happens
        self.assertFalse(os.path.exists(local_tmp_dir))
        self.assertFalse(any(runner.ls(runner.get_output_dir())))

        # job should get terminated
        emr_conn = runner.make_emr_conn()
        cluster_id = runner.get_cluster_id()
        for _ in range(10):
            emr_conn.simulate_progress(cluster_id)

        cluster = runner._describe_cluster()
        self.assertEqual(cluster.status.state, 'TERMINATED')

    def test_failed_job(self):
        mr_job = MRTwoStepJob(['-r', 'emr', '-v'])
        mr_job.sandbox()

        self.add_mock_s3_data({'walrus': {}})
        self.mock_emr_failures = {('j-MOCKCLUSTER0', 0): None}

        with no_handlers_for_logger('mrjob.emr'):
            stderr = StringIO()
            log_to_stream('mrjob.emr', stderr)

            with mr_job.make_runner() as runner:
                self.assertIsInstance(runner, EMRJobRunner)

                self.assertRaises(Exception, runner.run)
                # make sure job flow ID printed in error string
                self.assertIn('Job on job flow j-MOCKCLUSTER0 failed',
                              stderr.getvalue())

                emr_conn = runner.make_emr_conn()
                cluster_id = runner.get_cluster_id()
                for _ in range(10):
                    emr_conn.simulate_progress(cluster_id)

                cluster = runner._describe_cluster()
                self.assertEqual(cluster.status.state,
                                 'TERMINATED_WITH_ERRORS')

            # job should get terminated on cleanup
            cluster_id = runner.get_cluster_id()
            for _ in range(10):
                emr_conn.simulate_progress(cluster_id)

        cluster = runner._describe_cluster()
        self.assertEqual(cluster.status.state, 'TERMINATED_WITH_ERRORS')

    def _test_remote_scratch_cleanup(self, mode, scratch_len, log_len):
        self.add_mock_s3_data({'walrus': {'logs/j-MOCKCLUSTER0/1': b'1\n'}})
        stdin = BytesIO(b'foo\nbar\n')

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--s3-log-uri', 's3://walrus/logs',
                               '-', '--cleanup', mode])
        mr_job.sandbox(stdin=stdin)

        with mr_job.make_runner() as runner:
            s3_scratch_uri = runner._opts['s3_scratch_uri']
            scratch_bucket, _ = parse_s3_uri(s3_scratch_uri)

            runner.run()

            # this is set and unset before we can get at it unless we do this
            log_bucket, _ = parse_s3_uri(runner._s3_job_log_uri)

            list(runner.stream_output())

        conn = runner.make_s3_conn()
        bucket = conn.get_bucket(scratch_bucket)
        self.assertEqual(len(list(bucket.list())), scratch_len)

        bucket = conn.get_bucket(log_bucket)
        self.assertEqual(len(list(bucket.list())), log_len)

    def test_cleanup_all(self):
        self._test_remote_scratch_cleanup('ALL', 0, 0)

    def test_cleanup_scratch(self):
        self._test_remote_scratch_cleanup('SCRATCH', 0, 1)

    def test_cleanup_remote(self):
        self._test_remote_scratch_cleanup('REMOTE_SCRATCH', 0, 1)

    def test_cleanup_local(self):
        self._test_remote_scratch_cleanup('LOCAL_SCRATCH', 5, 1)

    def test_cleanup_logs(self):
        self._test_remote_scratch_cleanup('LOGS', 5, 0)

    def test_cleanup_none(self):
        self._test_remote_scratch_cleanup('NONE', 5, 1)

    def test_cleanup_combine(self):
        self._test_remote_scratch_cleanup('LOGS,REMOTE_SCRATCH', 0, 0)

    def test_cleanup_error(self):
        self.assertRaises(ValueError, self._test_remote_scratch_cleanup,
                          'NONE,LOGS,REMOTE_SCRATCH', 0, 0)
        self.assertRaises(ValueError, self._test_remote_scratch_cleanup,
                          'GARBAGE', 0, 0)

    def test_wait_for_cluster_to_terminate(self):
        # Test regression from #338 where _wait_for_cluster_to_terminate()
        # (called _wait_for_job_flow_termination() at the time)
        # would raise an IndexError whenever the job flow wasn't already
        # finished
        mr_job = MRTwoStepJob(['-r', 'emr'])
        mr_job.sandbox()
        with mr_job.make_runner() as runner:
            runner._add_job_files_for_upload()
            runner._launch_emr_job()
            runner._wait_for_cluster_to_terminate()




class ExistingClusterTestCase(MockBotoTestCase):

    def test_attach_to_existing_cluster(self):
        emr_conn = EMRJobRunner(conf_paths=[]).make_emr_conn()
        # set log_uri to None, so that when we describe the job flow, it
        # won't have the loguri attribute, to test Issue #112
        cluster_id = emr_conn.run_jobflow(
            name='Development Cluster', log_uri=None,
            keep_alive=True, job_flow_role='fake-instance-profile',
            service_role='fake-service-role')

        stdin = BytesIO(b'foo\nbar\n')
        self.mock_emr_output = {(cluster_id, 1): [
            b'1\t"bar"\n1\t"foo"\n2\tnull\n']}

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--emr-job-flow-id', cluster_id])
        mr_job.sandbox(stdin=stdin)

        results = []
        with mr_job.make_runner() as runner:
            runner.run()

            # Issue 182: don't create the bootstrap script when
            # attaching to another job flow
            self.assertIsNone(runner._master_bootstrap_script_path)

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

        self.assertEqual(sorted(results),
                         [(1, 'bar'), (1, 'foo'), (2, None)])

    def test_dont_take_down_cluster_on_failure(self):
        emr_conn = EMRJobRunner(conf_paths=[]).make_emr_conn()
        # set log_uri to None, so that when we describe the job flow, it
        # won't have the loguri attribute, to test Issue #112
        cluster_id = emr_conn.run_jobflow(
            name='Development Cluster', log_uri=None,
            keep_alive=True, job_flow_role='fake-instance-profile',
            service_role='fake-service-role')

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--emr-job-flow-id', cluster_id])
        mr_job.sandbox()

        self.add_mock_s3_data({'walrus': {}})
        self.mock_emr_failures = set([('j-MOCKCLUSTER0', 0)])

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, EMRJobRunner)
            self.prepare_runner_for_ssh(runner)
            with logger_disabled('mrjob.emr'):
                self.assertRaises(Exception, runner.run)

            emr_conn = runner.make_emr_conn()
            cluster_id = runner.get_cluster_id()
            for _ in range(10):
                emr_conn.simulate_progress(cluster_id)

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.status.state, 'WAITING')

        # job shouldn't get terminated by cleanup
        emr_conn = runner.make_emr_conn()
        cluster_id = runner.get_cluster_id()
        for _ in range(10):
            emr_conn.simulate_progress(cluster_id)

        cluster = runner._describe_cluster()
        self.assertEqual(cluster.status.state, 'WAITING')


class VisibleToAllUsersTestCase(MockBotoTestCase):

    def test_defaults(self):
        cluster = self.run_and_get_cluster()
        self.assertEqual(cluster.visibletoallusers, 'true')

    def test_no_visible(self):
        cluster = self.run_and_get_cluster('--no-visible-to-all-users')
        self.assertEqual(cluster.visibletoallusers, 'false')

    def test_force_to_bool(self):
        # make sure mockboto doesn't always convert to bool
        self.assertRaises(boto.exception.EmrResponseError,
            self.run_and_get_cluster,
            '--emr-api-param', 'VisibleToAllUsers=1')

    def test_visible(self):
        cluster = self.run_and_get_cluster('--visible-to-all-users')
        self.assertTrue(cluster.visibletoallusers, 'true')

        VISIBLE_MRJOB_CONF = {'runners': {'emr': {
            'check_emr_status_every': 0.00,
            's3_sync_wait_time': 0.00,
            'visible_to_all_users': 1,  # should be True
        }}}

        with mrjob_conf_patcher(VISIBLE_MRJOB_CONF):
            visible_cluster = self.run_and_get_cluster()
            self.assertEqual(visible_cluster.visibletoallusers, 'true')


class IAMTestCase(MockBotoTestCase):

    def setUp(self):
        super(IAMTestCase, self).setUp()

        # wrap connect_iam() so we can see if it was called
        p_iam = patch.object(boto, 'connect_iam', wraps=boto.connect_iam)
        self.addCleanup(p_iam.stop)
        p_iam.start()

    def test_role_auto_creation(self):
        cluster = self.run_and_get_cluster()
        self.assertTrue(boto.connect_iam.called)

        # check instance_profile
        instance_profile_name = (
            cluster.ec2instanceattributes.iaminstanceprofile)
        self.assertIsNotNone(instance_profile_name)
        self.assertTrue(instance_profile_name.startswith('mrjob-'))
        self.assertIn(instance_profile_name, self.mock_iam_instance_profiles)
        self.assertIn(instance_profile_name, self.mock_iam_roles)
        self.assertIn(instance_profile_name,
                      self.mock_iam_role_attached_policies)

        # check service_role
        service_role_name = cluster.servicerole
        self.assertIsNotNone(service_role_name)
        self.assertTrue(service_role_name.startswith('mrjob-'))
        self.assertIn(service_role_name, self.mock_iam_roles)
        self.assertIn(service_role_name,
                      self.mock_iam_role_attached_policies)

        # instance_profile and service_role should be distinct
        self.assertNotEqual(instance_profile_name, service_role_name)

        # run again, and see if we reuse the roles
        cluster2 = self.run_and_get_cluster()
        self.assertEqual(cluster2.ec2instanceattributes.iaminstanceprofile,
                         instance_profile_name)
        self.assertEqual(cluster2.servicerole, service_role_name)

    def test_iam_instance_profile_option(self):
        cluster = self.run_and_get_cluster(
            '--iam-instance-profile', 'EMR_EC2_DefaultRole')
        self.assertTrue(boto.connect_iam.called)

        self.assertEqual(cluster.ec2instanceattributes.iaminstanceprofile,
                         'EMR_EC2_DefaultRole')

    def test_iam_service_role_option(self):
        cluster = self.run_and_get_cluster(
            '--iam-service-role', 'EMR_DefaultRole')
        self.assertTrue(boto.connect_iam.called)

        self.assertEqual(cluster.servicerole, 'EMR_DefaultRole')

    def test_both_iam_options(self):
        cluster = self.run_and_get_cluster(
            '--iam-instance-profile', 'EMR_EC2_DefaultRole',
            '--iam-service-role', 'EMR_DefaultRole')

        # users with limited access may not be able to connect to the IAM API.
        # This gives them a plan B
        self.assertFalse(boto.connect_iam.called)

        self.assertEqual(cluster.ec2instanceattributes.iaminstanceprofile,
                         'EMR_EC2_DefaultRole')
        self.assertEqual(cluster.servicerole, 'EMR_DefaultRole')

    def test_no_iam_access(self):
        ex = boto.exception.BotoServerError(403, 'Forbidden')
        self.assertIsInstance(boto.connect_iam, Mock)
        boto.connect_iam.side_effect = ex

        with logger_disabled('mrjob.emr'):
            cluster = self.run_and_get_cluster()

        self.assertTrue(boto.connect_iam.called)

        self.assertEqual(cluster.ec2instanceattributes.iaminstanceprofile,
                         'EMR_EC2_DefaultRole')
        self.assertEqual(cluster.servicerole, 'EMR_DefaultRole')


class EMRAPIParamsTestCase(MockBotoTestCase):

    def test_param_set(self):
        cluster = self.run_and_get_cluster(
            '--emr-api-param', 'Test.API=a', '--emr-api-param', 'Test.API2=b')
        self.assertTrue('Test.API' in cluster._api_params)
        self.assertTrue('Test.API2' in cluster._api_params)
        self.assertEqual(cluster._api_params['Test.API'], 'a')
        self.assertEqual(cluster._api_params['Test.API2'], 'b')

    def test_param_unset(self):
        cluster = self.run_and_get_cluster(
            '--no-emr-api-param', 'Test.API', '--no-emr-api-param', 'Test.API2')
        self.assertTrue('Test.API' in cluster._api_params)
        self.assertTrue('Test.API2' in cluster._api_params)
        self.assertIsNone(cluster._api_params['Test.API'])
        self.assertIsNone(cluster._api_params['Test.API2'])

    def test_invalid_param(self):
        self.assertRaises(
            ValueError, self.run_and_get_cluster,
            '--emr-api-param', 'Test.API')

    def test_overrides(self):
        cluster = self.run_and_get_cluster(
            '--emr-api-param', 'VisibleToAllUsers=false',
            '--visible-to-all-users')
        self.assertEqual(cluster.visibletoallusers, 'false')

    def test_no_emr_api_param_command_line_switch(self):
        job = MRWordCount([
            '-r', 'emr',
            '--emr-api-param', 'Instances.Ec2SubnetId=someID',
            '--no-emr-api-param', 'VisibleToAllUsers'])

        with job.make_runner() as runner:
            self.assertEqual(runner._opts['emr_api_params'],
                             {'Instances.Ec2SubnetId': 'someID',
                              'VisibleToAllUsers': None})

    def test_no_emr_api_params_is_not_a_real_option(self):
        job = MRWordCount([
            '-r', 'emr',
            '--no-emr-api-param', 'VisibleToAllUsers'])

        self.assertNotIn('no_emr_api_params',
                         sorted(job.emr_job_runner_kwargs()))
        self.assertNotIn('no_emr_api_param',
                         sorted(job.emr_job_runner_kwargs()))

        with job.make_runner() as runner:
            self.assertNotIn('no_emr_api_params', sorted(runner._opts))
            self.assertNotIn('no_emr_api_param', sorted(runner._opts))
            self.assertEqual(runner._opts['emr_api_params'],
                            {'VisibleToAllUsers': None})

    def test_command_line_overrides_config(self):
        # want to make sure a nulled-out param in the config file
        # can't override a param set on the command line

        API_PARAMS_MRJOB_CONF = {'runners': {'emr': {
            'check_emr_status_every': 0.00,
            's3_sync_wait_time': 0.00,
            'emr_api_params': {
                'Instances.Ec2SubnetId': 'someID',
                'VisibleToAllUsers': None,
                'Name': 'eaten_by_a_whale',
            },
        }}}

        job = MRWordCount([
            '-r', 'emr',
            '--no-emr-api-param', 'Instances.Ec2SubnetId',
            '--emr-api-param', 'VisibleToAllUsers=true'])

        with mrjob_conf_patcher(API_PARAMS_MRJOB_CONF):
            with job.make_runner() as runner:
                self.assertEqual(runner._opts['emr_api_params'],
                    {'Instances.Ec2SubnetId': None,
                     'VisibleToAllUsers': 'true',
                     'Name': 'eaten_by_a_whale'})


class AMIAndHadoopVersionTestCase(MockBotoTestCase):

    def test_default(self):
        with self.make_runner() as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), _DEFAULT_AMI_VERSION)
            self.assertEqual(runner.get_hadoop_version(), '2.4.0')

    def test_ami_version_1_0_no_longer_supported(self):
        with self.make_runner('--ami-version', '1.0') as runner:
            self.assertRaises(boto.exception.EmrResponseError,
                              runner._launch)

    def test_ami_version_2_0(self):
        with self.make_runner('--ami-version', '2.0') as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), '2.0.6')
            self.assertEqual(runner.get_hadoop_version(), '0.20.205')

    def test_latest_ami_version(self):
        # "latest" is no longer actually the latest version
        with self.make_runner('--ami-version', 'latest') as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), '2.4.2')
            self.assertEqual(runner.get_hadoop_version(), '1.0.3')

    def test_ami_version_3_0(self):
        with self.make_runner('--ami-version', '3.0',
                              '--ec2-instance-type', 'm1.medium') as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), '3.0.4')
            self.assertEqual(runner.get_hadoop_version(), '2.2.0')

    def test_ami_version_3_8_0(self):
        with self.make_runner('--ami-version', '3.8.0',
                              '--ec2-instance-type', 'm1.medium') as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), '3.8.0')
            self.assertEqual(runner.get_hadoop_version(), '2.4.0')

    def test_hadoop_version_option_does_nothing(self):
        with logger_disabled('mrjob.emr'):
            with self.make_runner('--hadoop-version', '1.2.3.4') as runner:
                runner.run()
                self.assertEqual(runner.get_ami_version(),
                                 _DEFAULT_AMI_VERSION)
                self.assertEqual(runner.get_hadoop_version(), '2.4.0')


class AvailabilityZoneTestCase(MockBotoTestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'emr': {
        'check_emr_status_every': 0.00,
        's3_sync_wait_time': 0.00,
        'aws_availability_zone': 'PUPPYLAND',
    }}}

    def test_availability_zone_config(self):
        with self.make_runner() as runner:
            runner.run()

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.ec2instanceattributes.ec2availabilityzone,
                             'PUPPYLAND')


class EnableDebuggingTestCase(MockBotoTestCase):

    def test_debugging_works(self):
        with self.make_runner('--enable-emr-debugging') as runner:
            runner.run()

            emr_conn = runner.make_emr_conn()
            steps = list(_yield_all_steps(emr_conn, runner.get_cluster_id()))

            self.assertEqual(steps[0].name, 'Setup Hadoop Debugging')


class RegionTestCase(MockBotoTestCase):

    def test_default(self):
        runner = EMRJobRunner()
        self.assertEqual(runner._opts['aws_region'], 'us-west-2')

    def test_explicit_region(self):
        runner = EMRJobRunner(aws_region='us-east-1')
        self.assertEqual(runner._opts['aws_region'], 'us-east-1')

    def test_cannot_be_empty(self):
        runner = EMRJobRunner(aws_region='')
        self.assertEqual(runner._opts['aws_region'], 'us-west-2')


class ScratchBucketTestCase(MockBotoTestCase):

    def assert_new_scratch_bucket(self, location, **runner_kwargs):
        """Assert that if we create an EMRJobRunner with the given keyword
        args, it'll create a new scratch bucket with the given location
        constraint.
        """
        existing_bucket_names = set(self.mock_s3_fs)

        runner = EMRJobRunner(conf_paths=[], **runner_kwargs)
        runner._create_s3_temp_bucket_if_needed()

        bucket_name, path = parse_s3_uri(runner._opts['s3_scratch_uri'])

        self.assertTrue(bucket_name.startswith('mrjob-'))
        self.assertNotIn(bucket_name, existing_bucket_names)
        self.assertEqual(path, 'tmp/')

        self.assertEqual(self.mock_s3_fs[bucket_name]['location'], location)

    def test_default(self):
        self.assert_new_scratch_bucket('us-west-2')

    def test_us_west_1(self):
        self.assert_new_scratch_bucket('us-west-1',
                                       aws_region='us-west-1')

    def test_us_east_1(self):
        # location should be blank
        self.assert_new_scratch_bucket('',
                                       aws_region='us-east-1')

    def test_reuse_mrjob_bucket_in_same_region(self):
        self.add_mock_s3_data({'mrjob-1': {}}, location='us-west-2')

        runner = EMRJobRunner()
        self.assertEqual(runner._opts['s3_scratch_uri'],
                         's3://mrjob-1/tmp/')

    def test_ignore_mrjob_bucket_in_different_region(self):
        # this tests 687
        self.add_mock_s3_data({'mrjob-1': {}}, location='')

        self.assert_new_scratch_bucket('us-west-2')

    def test_ignore_non_mrjob_bucket_in_different_region(self):
        self.add_mock_s3_data({'walrus': {}}, location='us-west-2')

        self.assert_new_scratch_bucket('us-west-2')

    def test_reuse_mrjob_bucket_in_us_east_1(self):
        # us-east-1 is special because the location "constraint" for its
        # buckets is '', not 'us-east-1'
        self.add_mock_s3_data({'mrjob-1': {}}, location='')

        runner = EMRJobRunner(aws_region='us-east-1')

        self.assertEqual(runner._opts['s3_scratch_uri'],
                         's3://mrjob-1/tmp/')

    def test_explicit_scratch_uri(self):
        self.add_mock_s3_data({'walrus': {}}, location='us-west-2')

        runner = EMRJobRunner(s3_scratch_uri='s3://walrus/tmp/')

        self.assertEqual(runner._opts['s3_scratch_uri'],
                         's3://walrus/tmp/')

    def test_cross_region_explicit_scratch_uri(self):
        self.add_mock_s3_data({'walrus': {}}, location='us-west-2')

        runner = EMRJobRunner(aws_region='us-west-1',
                              s3_scratch_uri='s3://walrus/tmp/')

        self.assertEqual(runner._opts['s3_scratch_uri'],
                         's3://walrus/tmp/')

        # scratch bucket shouldn't influence aws_region (it did in 0.4.x)
        self.assertEqual(runner._opts['aws_region'], 'us-west-1')


class EC2InstanceGroupTestCase(MockBotoTestCase):

    maxDiff = None

    def _test_instance_groups(self, opts, **expected):
        """Run a job with the given option dictionary, and check for
        for instance, number, and optional bid price for each instance role.

        Specify expected instance group info like:

        <role>=(num_instances, instance_type, bid_price)
        """
        runner = EMRJobRunner(**opts)
        cluster_id = runner.make_persistent_cluster()

        emr_conn = runner.make_emr_conn()
        instance_groups = list(
            _yield_all_instance_groups(emr_conn, cluster_id))

        # convert actual instance groups to dicts
        role_to_actual = {}
        for ig in instance_groups:
            info = dict(
                (field, getattr(ig, field, None))
                for field in ('bidprice', 'instancetype',
                              'market', 'requestedinstancecount'))

            role_to_actual[ig.instancegrouptype] = info

        # convert expected to dicts
        role_to_expected = {}
        for role, (num, instance_type, bid_price) in expected.items():
            info = dict(
                bidprice=(bid_price if bid_price else None),
                instancetype=instance_type,
                market=(u'SPOT' if bid_price else u'ON_DEMAND'),
                requestedinstancecount=str(num),
            )

            role_to_expected[role.upper()] = info

        self.assertEqual(role_to_actual, role_to_expected)

    def set_in_mrjob_conf(self, **kwargs):
        emr_opts = copy.deepcopy(self.MRJOB_CONF_CONTENTS)
        emr_opts['runners']['emr'].update(kwargs)
        patcher = mrjob_conf_patcher(emr_opts)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults(self):
        self._test_instance_groups(
            {},
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'num_ec2_instances': 3},
            core=(2, 'm1.medium', None),
            master=(1, 'm1.medium', None))

    def test_single_instance(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            master=(1, 'c1.xlarge', None))

    def test_multiple_instances(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge', 'num_ec2_instances': 3},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.medium', None))

    def test_explicit_master_and_slave_instance_types(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

    def test_explicit_instance_types_take_precedence(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge',
             'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge',
             'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

    def test_cmd_line_opts_beat_mrjob_conf(self):
        # set ec2_instance_type in mrjob.conf, 1 instance
        self.set_in_mrjob_conf(ec2_instance_type='c1.xlarge')

        self._test_instance_groups(
            {},
            master=(1, 'c1.xlarge', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        # set ec2_instance_type in mrjob.conf, 3 instances
        self.set_in_mrjob_conf(ec2_instance_type='c1.xlarge',
                               num_ec2_instances=3)

        self._test_instance_groups(
            {},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge'},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

        # set master in mrjob.conf, 1 instance
        self.set_in_mrjob_conf(ec2_master_instance_type='m1.large')

        self._test_instance_groups(
            {},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            master=(1, 'c1.xlarge', None))

        # set master and slave in mrjob.conf, 2 instances
        self.set_in_mrjob_conf(ec2_master_instance_type='m1.large',
                               ec2_slave_instance_type='m2.xlarge',
                               num_ec2_instances=3)

        self._test_instance_groups(
            {},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.large', None))

    def test_zero_core_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'c1.medium',
             'num_ec2_core_instances': 0},
            master=(1, 'c1.medium', None))

    def test_core_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 5},
            core=(5, 'c1.medium', '0.20'),
            master=(1, 'm1.large', None))

    def test_core_on_demand_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5},
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None))

        # Test the ec2_slave_instance_type alias
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'c1.medium',
             'num_ec2_instances': 6},
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None))

    def test_core_and_task_on_demand_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5,
             'ec2_task_instance_type': 'm2.xlarge',
             'num_ec2_task_instances': 20,
             },
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', None))

    def test_core_and_task_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 10,
             'ec2_task_instance_type': 'm2.xlarge',
             'ec2_task_instance_bid_price': '1.00',
             'num_ec2_task_instances': 20,
             },
            core=(10, 'c1.medium', '0.20'),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', '1.00'))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 10,
             'ec2_task_instance_type': 'm2.xlarge',
             'ec2_task_instance_bid_price': '1.00',
             'num_ec2_task_instances': 20,
             },
            core=(10, 'c1.medium', None),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', '1.00'))

    def test_master_and_core_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_master_instance_bid_price': '0.50',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 10,
             },
            core=(10, 'c1.medium', '0.20'),
            master=(1, 'm1.large', '0.50'))

    def test_master_spot_instance(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_master_instance_bid_price': '0.50',
             },
            master=(1, 'm1.large', '0.50'))

    def test_zero_or_blank_bid_price_means_on_demand(self):
        self._test_instance_groups(
            {'ec2_master_instance_bid_price': '0',
             },
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'num_ec2_core_instances': 3,
             'ec2_core_instance_bid_price': '0.00',
             },
            core=(3, 'm1.medium', None),
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'num_ec2_core_instances': 3,
             'num_ec2_task_instances': 5,
             'ec2_task_instance_bid_price': '',
             },
            core=(3, 'm1.medium', None),
            master=(1, 'm1.medium', None),
            task=(5, 'm1.medium', None))

    def test_pass_invalid_bid_prices_through_to_emr(self):
        self.assertRaises(
            boto.exception.EmrResponseError,
            self._test_instance_groups,
            {'ec2_master_instance_bid_price': 'all the gold in California'})

    def test_task_type_defaults_to_core_type(self):
        self._test_instance_groups(
            {'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5,
             'num_ec2_task_instances': 20,
             },
            core=(5, 'c1.medium', None),
            master=(1, 'm1.medium', None),
            task=(20, 'c1.medium', None))

    def test_mixing_instance_number_opts_on_cmd_line(self):
        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.emr', stderr)
            self._test_instance_groups(
                {'num_ec2_instances': 4,
                 'num_ec2_core_instances': 10},
                core=(10, 'm1.medium', None),
                master=(1, 'm1.medium', None))

        self.assertIn('does not make sense', stderr.getvalue())

    def test_mixing_instance_number_opts_in_mrjob_conf(self):
        self.set_in_mrjob_conf(num_ec2_instances=3,
                               num_ec2_core_instances=5,
                               num_ec2_task_instances=9)

        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.emr', stderr)
            self._test_instance_groups(
                {},
                core=(5, 'm1.medium', None),
                master=(1, 'm1.medium', None),
                task=(9, 'm1.medium', None))

        self.assertIn('does not make sense', stderr.getvalue())

    def test_cmd_line_instance_numbers_beat_mrjob_conf(self):
        self.set_in_mrjob_conf(num_ec2_core_instances=5,
                               num_ec2_task_instances=9)

        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.emr', stderr)
            self._test_instance_groups(
                {'num_ec2_instances': 3},
                core=(2, 'm1.medium', None),
                master=(1, 'm1.medium', None))

        self.assertNotIn('does not make sense', stderr.getvalue())


### tests for error parsing ###

BUCKET = 'walrus'
BUCKET_URI = 's3://' + BUCKET + '/'

LOG_DIR = 'j-JOBFLOWID/'

GARBAGE = \
b"""GarbageGarbageGarbage
"""

TRACEBACK_START = b'Traceback (most recent call last):\n'

PY_EXCEPTION = \
b"""  File "<string>", line 1, in <module>
TypeError: 'int' object is not iterable
"""

CHILD_ERR_LINE = (
    b'2010-07-27 18:25:48,397 WARN'
    b' org.apache.hadoop.mapred.TaskTracker (main): Error running child\n')

JAVA_STACK_TRACE = b"""java.lang.OutOfMemoryError: Java heap space
        at org.apache.hadoop.mapred.IFile$Reader.readNextBlock(IFile.java:270)
        at org.apache.hadoop.mapred.IFile$Reader.next(IFile.java:332)
"""

HADOOP_ERR_LINE_PREFIX = (b'2010-07-27 19:53:35,451 ERROR'
                          b' org.apache.hadoop.streaming.StreamJob (main): ')

USEFUL_HADOOP_ERROR = (
    b'Error launching job , Output path already exists :'
    b' Output directory s3://yourbucket/logs/2010/07/23/ already exists'
    b' and is not empty')

BORING_HADOOP_ERROR = b'Job not Successful!'
TASK_ATTEMPTS_DIR = LOG_DIR + 'task-attempts/'

ATTEMPT_0_DIR = TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000126_0/'
ATTEMPT_1_DIR = TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000126_0/'


def make_input_uri_line(input_uri):
    return ("2010-07-27 17:55:29,400 INFO"
            " org.apache.hadoop.fs.s3native.NativeS3FileSystem (main):"
            " Opening '%s' for reading\n" % input_uri).encode('utf_8')


class FindProbableCauseOfFailureTestCase(MockBotoTestCase):

    def setUp(self):
        super(FindProbableCauseOfFailureTestCase, self).setUp()
        self.make_runner()

    def tearDown(self):
        self.cleanup_runner()
        super(FindProbableCauseOfFailureTestCase, self).tearDown()

    # We're mostly concerned here that the right log files are read in the
    # right order. parsing of the logs is handled by tests.parse_test
    def make_runner(self):
        self.add_mock_s3_data({'walrus': {}})
        self.runner = EMRJobRunner(s3_sync_wait_time=0,
                                   s3_scratch_uri='s3://walrus/tmp',
                                   conf_paths=[])
        self.runner._s3_job_log_uri = BUCKET_URI + LOG_DIR

    def cleanup_runner(self):
        self.runner.cleanup()

    def test_empty(self):
        self.add_mock_s3_data({'walrus': {}})
        self.assertEqual(self.runner._find_probable_cause_of_failure([1]),
                         None)

    def test_python_exception(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr':
                GARBAGE + TRACEBACK_START + PY_EXCEPTION + GARBAGE,
            ATTEMPT_0_DIR + 'syslog':
                make_input_uri_line(BUCKET_URI + 'input.gz'),
        }})
        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1]),
            {'lines': (TRACEBACK_START + PY_EXCEPTION).decode(
                'ascii').splitlines(True),
             'log_file_uri': BUCKET_URI + ATTEMPT_0_DIR + 'stderr',
             'input_uri': BUCKET_URI + 'input.gz'})

    def test_python_exception_without_input_uri(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr': (
                GARBAGE + TRACEBACK_START + PY_EXCEPTION + GARBAGE),
        }})
        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1]),
            {'lines': (TRACEBACK_START + PY_EXCEPTION).decode(
                'ascii').splitlines(True),
             'log_file_uri': BUCKET_URI + ATTEMPT_0_DIR + 'stderr',
             'input_uri': None})

    def test_java_exception(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr': GARBAGE + GARBAGE,
            ATTEMPT_0_DIR + 'syslog':
                make_input_uri_line(BUCKET_URI + 'input.gz') +
                GARBAGE +
                CHILD_ERR_LINE +
                JAVA_STACK_TRACE +
                GARBAGE,
        }})
        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1]),
            {'lines': JAVA_STACK_TRACE.decode('ascii').splitlines(True),
             'log_file_uri': BUCKET_URI + ATTEMPT_0_DIR + 'syslog',
             'input_uri': BUCKET_URI + 'input.gz'})

    def test_java_exception_without_input_uri(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'syslog':
                CHILD_ERR_LINE +
                JAVA_STACK_TRACE +
                GARBAGE,
        }})
        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1]),
            {'lines': JAVA_STACK_TRACE.decode('ascii').splitlines(True),
             'log_file_uri': BUCKET_URI + ATTEMPT_0_DIR + 'syslog',
             'input_uri': None})

    def test_hadoop_streaming_error(self):
        # we should look only at step 2 since the errors in the other
        # steps are boring
        #
        # we include input.gz just to test that we DON'T check for it
        self.add_mock_s3_data({'walrus': {
            LOG_DIR + 'steps/1/syslog':
                GARBAGE +
                HADOOP_ERR_LINE_PREFIX + BORING_HADOOP_ERROR + b'\n',
            LOG_DIR + 'steps/2/syslog':
                GARBAGE +
                make_input_uri_line(BUCKET_URI + 'input.gz') +
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + b'\n',
            LOG_DIR + 'steps/3/syslog':
                HADOOP_ERR_LINE_PREFIX + BORING_HADOOP_ERROR + b'\n',
        }})

        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1, 2, 3]),
            {'lines': [(USEFUL_HADOOP_ERROR + b'\n').decode('ascii')],
             'log_file_uri': BUCKET_URI + LOG_DIR + 'steps/2/syslog',
             'input_uri': None})

    def test_later_task_attempt_steps_win(self):
        # should look at later steps first
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_r_000126_3/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000004_0/syslog':
                CHILD_ERR_LINE + JAVA_STACK_TRACE,
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0002_m_000004_0/syslog')

    def test_later_step_logs_win(self):
        self.add_mock_s3_data({'walrus': {
            LOG_DIR + 'steps/1/syslog':
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + b'\n',
            LOG_DIR + 'steps/2/syslog':
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + b'\n',
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + LOG_DIR + 'steps/2/syslog')

    def test_reducer_beats_mapper(self):
        # should look at reducers over mappers
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000126_3/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_r_000126_3/syslog':
                CHILD_ERR_LINE + JAVA_STACK_TRACE,
        }})
        failure = self.runner._find_probable_cause_of_failure([1])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0001_r_000126_3/syslog')

    def test_more_attempts_win(self):
        # look at fourth attempt before looking at first attempt
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000004_3/syslog':
                CHILD_ERR_LINE + JAVA_STACK_TRACE,
        }})
        failure = self.runner._find_probable_cause_of_failure([1])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0001_m_000004_3/syslog')

    def test_py_exception_beats_java_stack_trace(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr': TRACEBACK_START + PY_EXCEPTION,
            ATTEMPT_0_DIR + 'syslog': CHILD_ERR_LINE + JAVA_STACK_TRACE,
        }})
        failure = self.runner._find_probable_cause_of_failure([1])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + ATTEMPT_0_DIR + 'stderr')

    def test_exception_beats_hadoop_error(self):
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            LOG_DIR + 'steps/1/syslog':
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + b'\n',
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0002_m_000126_0/stderr')

    def test_step_filtering(self):
        # same as previous test, but step 2 is filtered out
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            LOG_DIR + 'steps/1/syslog':
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + b'\n',
        }})
        failure = self.runner._find_probable_cause_of_failure([1])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + LOG_DIR + 'steps/1/syslog')

    def test_ignore_errors_from_steps_that_later_succeeded(self):
        # This tests the fix for Issue #31
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr':
                GARBAGE + TRACEBACK_START + PY_EXCEPTION + GARBAGE,
            ATTEMPT_0_DIR + 'syslog':
                make_input_uri_line(BUCKET_URI + 'input.gz'),
            ATTEMPT_1_DIR + 'stderr': b'',
            ATTEMPT_1_DIR + 'syslog':
                make_input_uri_line(BUCKET_URI + 'input.gz'),
        }})
        self.assertEqual(self.runner._find_probable_cause_of_failure([1]),
                         None)


class CounterFetchingTestCase(MockBotoTestCase):

    COUNTER_LINE = (
        b'Job JOBID="job_201106092314_0001" FINISH_TIME="1307662284564"'
        b' JOB_STATUS="SUCCESS" FINISHED_MAPS="0" FINISHED_REDUCES="0"'
        b' FAILED_MAPS="0" FAILED_REDUCES="0" COUNTERS="'
        b'{(org\.apache\.hadoop\.mapred\.JobInProgress$Counter)'
        b'(Job Counters )'
        b'[(TOTAL_LAUNCHED_REDUCES)(Launched reduce tasks)(1)]}'
        b'" .')

    def setUp(self):
        super(CounterFetchingTestCase, self).setUp()
        self.add_mock_s3_data({'walrus': {}})
        kwargs = {
            'conf_paths': [],
            's3_scratch_uri': 's3://walrus/',
            's3_sync_wait_time': 0}
        with EMRJobRunner(**kwargs) as runner:
            cluster_id = runner.make_persistent_cluster()

        self.runner = EMRJobRunner(emr_job_flow_id=cluster_id, **kwargs)
        self.mock_cluster = self.mock_emr_clusters[cluster_id]

    def tearDown(self):
        super(CounterFetchingTestCase, self).tearDown()
        self.runner.cleanup()

    def test_empty_counters_running_job(self):
        self.mock_cluster.status.state = 'RUNNING'

        with no_handlers_for_logger():
            stderr = StringIO()
            log_to_stream('mrjob.emr', stderr)
            self.runner._fetch_counters([1], skip_s3_wait=True)
            self.assertIn('5 minutes', stderr.getvalue())

    def test_present_counters_running_job(self):
        self.add_mock_s3_data({'walrus': {
            'logs/j-MOCKCLUSTER0/jobs/job_0_1_hadoop_streamjob1.jar':
            self.COUNTER_LINE}})
        self.mock_cluster.status.state = 'RUNNING'

        self.runner._fetch_counters([1], skip_s3_wait=True)
        self.assertEqual(self.runner.counters(),
                         [{'Job Counters ': {'Launched reduce tasks': 1}}])

    def test_present_counters_terminated_job(self):
        self.add_mock_s3_data({'walrus': {
            'logs/j-MOCKCLUSTER0/jobs/job_0_1_hadoop_streamjob1.jar':
            self.COUNTER_LINE}})
        self.mock_cluster.status.state = 'TERMINATED'

        self.runner._fetch_counters([1], skip_s3_wait=True)
        self.assertEqual(self.runner.counters(),
                         [{'Job Counters ': {'Launched reduce tasks': 1}}])

    def test_present_counters_step_mismatch(self):
        self.add_mock_s3_data({'walrus': {
            'logs/j-MOCKCLUSTER0/jobs/job_0_1_hadoop_streamjob1.jar':
            self.COUNTER_LINE}})
        self.mock_cluster.status.state = 'RUNNING'

        self.runner._fetch_counters([2], {2: 1}, skip_s3_wait=True)
        self.assertEqual(self.runner.counters(),
                         [{'Job Counters ': {'Launched reduce tasks': 1}}])

    def _mock_step(self, jar):
        return MockEmrObject(
            config=MockEmrObject(jar=jar),
            name=self.runner._job_key,
            status=MockEmrObject(state='COMPLETED'))

    def test_no_log_generating_steps(self):
        self.mock_cluster.status.state = 'TERMINATED'
        self.mock_cluster._steps = [
            self._mock_step(jar='x.jar'),
            self._mock_step(jar='x.jar'),
        ]

        self.runner._fetch_counters_s3 = Mock(return_value={})

        self.runner._wait_for_job_to_complete()
        self.runner._fetch_counters_s3.assert_called_with([], False)

    def test_interleaved_log_generating_steps(self):
        self.mock_cluster.status.state = 'TERMINATED'
        self.mock_cluster._steps = [
            self._mock_step(jar='x.jar'),
            self._mock_step(jar='hadoop.streaming.jar'),
            self._mock_step(jar='x.jar'),
            self._mock_step(jar='hadoop.streaming.jar'),
        ]

        self.runner._fetch_counters_s3 = Mock(return_value={})

        self.runner._wait_for_job_to_complete()
        self.runner._fetch_counters_s3.assert_called_with([1, 2], False)


class LogFetchingFallbackTestCase(MockBotoTestCase):

    def setUp(self):
        super(LogFetchingFallbackTestCase, self).setUp()
        # Make sure that SSH and S3 are accessed when we expect them to be
        self.add_mock_s3_data({'walrus': {}})

        self.runner = EMRJobRunner(s3_scratch_uri='s3://walrus/tmp')
        self.runner._s3_job_log_uri = BUCKET_URI + LOG_DIR
        self.prepare_runner_for_ssh(self.runner)

    def tearDown(self):
        super(LogFetchingFallbackTestCase, self).tearDown()
        """This method assumes ``prepare_runner_for_ssh()`` was called. That
        method isn't a "proper" setup method because it requires different
        arguments for different tests.
        """
        self.runner.cleanup()

    def test_ssh_comes_first(self):
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/steps/1')
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/history')
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/userlogs')

        # Put a log file and error into SSH
        ssh_lone_log_path = posixpath.join(
            SSH_LOG_ROOT, 'steps', '1', 'syslog')
        mock_ssh_file('testmaster', ssh_lone_log_path,
                      HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + b'\n')

        # Put a 'more interesting' error in S3 to make sure that the
        # 'less interesting' one from SSH is read and S3 is never
        # looked at. This would never happen in reality because the
        # logs should be identical, but it makes for an easy test
        # of SSH overriding S3.
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         SSH_PREFIX + self.runner._address + ssh_lone_log_path)

    def test_ssh_works_with_slaves(self):
        self.add_slave()

        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/steps/1')
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/history')
        mock_ssh_dir(
            'testmaster!testslave0',
            SSH_LOG_ROOT + '/userlogs/attempt_201007271720_0002_m_000126_0')

        # Put a log file and error into SSH
        ssh_log_path = posixpath.join(SSH_LOG_ROOT, 'userlogs',
                                      'attempt_201007271720_0002_m_000126_0',
                                      'stderr')
        ssh_log_path_2 = posixpath.join(SSH_LOG_ROOT, 'userlogs',
                                        'attempt_201007271720_0002_m_000126_0',
                                        'syslog')
        mock_ssh_file('testmaster!testslave0', ssh_log_path,
                      TRACEBACK_START + PY_EXCEPTION)
        mock_ssh_file('testmaster!testslave0', ssh_log_path_2,
                      b'')
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         SSH_PREFIX + 'testmaster!testslave0' + ssh_log_path)

    def test_ssh_fails_to_s3(self):
        # the runner will try to use SSH and find itself unable to do so,
        # throwing a LogFetchError and triggering S3 fetching.
        self.runner._address = None

        # Put a different error into S3
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0002_m_000126_0/stderr')


class TestEMREndpoints(MockBotoTestCase):

    def test_default_region(self):
        runner = EMRJobRunner(conf_paths=[])
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.us-west-2.amazonaws.com')
        self.assertEqual(runner._opts['aws_region'], 'us-west-2')

    def test_none_region(self):
        # blank region should be treated the same as no region
        runner = EMRJobRunner(conf_paths=[], aws_region=None)
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.us-west-2.amazonaws.com')
        self.assertEqual(runner._opts['aws_region'], 'us-west-2')

    def test_blank_region(self):
        # blank region should be treated the same as no region
        runner = EMRJobRunner(conf_paths=[], aws_region='')
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.us-west-2.amazonaws.com')
        self.assertEqual(runner._opts['aws_region'], 'us-west-2')

    def test_eu(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='EU')
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.eu-west-1.amazonaws.com')

    def test_eu_case_insensitive(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='eu')
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.eu-west-1.amazonaws.com')

    def test_us_east_1(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='us-east-1')
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.us-east-1.amazonaws.com')

    def test_us_west_1(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='us-west-1')
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.us-west-1.amazonaws.com')

    def test_us_west_1_case_insensitive(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='US-West-1')
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.us-west-1.amazonaws.com')

    def test_ap_southeast_1(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='ap-southeast-1')
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.ap-southeast-1.amazonaws.com')

    def test_previously_unknown_region(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='lolcatnia-1')
        self.assertEqual(runner.make_emr_conn().host,
                         'elasticmapreduce.lolcatnia-1.amazonaws.com')

    def test_explicit_endpoints(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='EU',
                              s3_endpoint='s3-proxy', emr_endpoint='emr-proxy')
        self.assertEqual(runner.make_emr_conn().host, 'emr-proxy')

    def test_ssl_fallback_host(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='us-west-1')

        with patch.object(MockEmrConnection, 'STRICT_SSL', True):
            emr_conn = runner.make_emr_conn()
            self.assertEqual(emr_conn.host,
                             'elasticmapreduce.us-west-1.amazonaws.com')
            # this should still work
            self.assertEqual(list(_yield_all_clusters(emr_conn)), [])
            # but it's only because we've switched to the alternate hostname
            self.assertEqual(emr_conn.host,
                             'us-west-1.elasticmapreduce.amazonaws.com')

        # without SSL issues, we should stay on the same endpoint
        emr_conn = runner.make_emr_conn()
        self.assertEqual(emr_conn.host,
                         'elasticmapreduce.us-west-1.amazonaws.com')

        self.assertEqual(list(_yield_all_clusters(emr_conn)), [])
        self.assertEqual(emr_conn.host,
                         'elasticmapreduce.us-west-1.amazonaws.com')


class TestS3Ls(MockBotoTestCase):

    def test_s3_ls(self):
        self.add_mock_s3_data(
            {'walrus': {'one': b'', 'two': b'', 'three': b''}})

        runner = EMRJobRunner(s3_scratch_uri='s3://walrus/tmp',
                              conf_paths=[])

        self.assertEqual(set(runner._s3_ls('s3://walrus/')),
                         set(['s3://walrus/one',
                              's3://walrus/two',
                              's3://walrus/three',
                              ]))

        self.assertEqual(set(runner._s3_ls('s3://walrus/t')),
                         set(['s3://walrus/two',
                              's3://walrus/three',
                              ]))

        self.assertEqual(set(runner._s3_ls('s3://walrus/t/')),
                         set([]))

        # if we ask for a nonexistent bucket, we should get some sort
        # of exception (in practice, buckets with random names will
        # probably be owned by other people, and we'll get some sort
        # of permissions error)
        self.assertRaises(Exception, set, runner._s3_ls('s3://lolcat/'))


class TestSSHLs(MockBotoTestCase):

    def setUp(self):
        super(TestSSHLs, self).setUp()
        self.make_runner()

    def tearDown(self):
        super(TestSSHLs, self).tearDown()

    def make_runner(self):
        self.runner = EMRJobRunner(conf_paths=[])
        self.prepare_runner_for_ssh(self.runner)

    def test_ssh_ls(self):
        self.add_slave()

        mock_ssh_dir('testmaster', 'test')
        mock_ssh_file('testmaster', posixpath.join('test', 'one'), b'')
        mock_ssh_file('testmaster', posixpath.join('test', 'two'), b'')
        mock_ssh_dir('testmaster!testslave0', 'test')
        mock_ssh_file('testmaster!testslave0',
                      posixpath.join('test', 'three'), b'')

        self.assertEqual(
            sorted(self.runner.ls('ssh://testmaster/test')),
            ['ssh://testmaster/test/one', 'ssh://testmaster/test/two'])

        self.runner._enable_slave_ssh_access()

        self.assertEqual(
            list(self.runner.ls('ssh://testmaster!testslave0/test')),
            ['ssh://testmaster!testslave0/test/three'])

        # ls() is a generator, so the exception won't fire until we list() it
        self.assertRaises(IOError, list,
                          self.runner.ls('ssh://testmaster/does_not_exist'))


class TestNoBoto(TestCase):

    def setUp(self):
        self.blank_out_boto()

    def tearDown(self):
        self.restore_boto()

    def blank_out_boto(self):
        self._real_boto = mrjob.emr.boto
        mrjob.emr.boto = None
        mrjob.fs.s3.boto = None

    def restore_boto(self):
        mrjob.emr.boto = self._real_boto
        mrjob.fs.s3.boto = self._real_boto

    def test_init(self):
        # merely creating an EMRJobRunner should raise an exception
        # because it'll need to connect to S3 to set s3_scratch_uri
        self.assertRaises(ImportError, EMRJobRunner, conf_paths=[])


class TestMasterBootstrapScript(MockBotoTestCase):

    def setUp(self):
        super(TestMasterBootstrapScript, self).setUp()
        self.make_tmp_dir()

    def tearDown(self):
        super(TestMasterBootstrapScript, self).tearDown()
        self.rm_tmp_dir()

    def make_tmp_dir(self):
        self.tmp_dir = tempfile.mkdtemp()

    def rm_tmp_dir(self):
        shutil.rmtree(self.tmp_dir)

    def test_usr_bin_env(self):
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_mrjob=True,
                              sh_bin='bash -e')

        runner._add_bootstrap_files_for_upload()

        self.assertIsNotNone(runner._master_bootstrap_script_path)
        self.assertTrue(os.path.exists(runner._master_bootstrap_script_path))

        with open(runner._master_bootstrap_script_path) as f:
            lines = [line.rstrip() for line in f]

        self.assertEqual(lines[0], '#!/usr/bin/env bash -e')

    def test_create_master_bootstrap_script(self):
        # create a fake src tarball
        foo_py_path = os.path.join(self.tmp_dir, 'foo.py')
        with open(foo_py_path, 'w'):
            pass

        yelpy_tar_gz_path = os.path.join(self.tmp_dir, 'yelpy.tar.gz')
        tar_and_gzip(self.tmp_dir, yelpy_tar_gz_path, prefix='yelpy')

        # use all the bootstrap options
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap=[
                                  PYTHON_BIN + ' ' +
                                  foo_py_path + '#bar.py',
                                  's3://walrus/scripts/ohnoes.sh#'],
                              bootstrap_cmds=['echo "Hi!"', 'true', 'ls'],
                              bootstrap_files=['/tmp/quz'],
                              bootstrap_mrjob=True,
                              bootstrap_python_packages=[yelpy_tar_gz_path],
                              bootstrap_scripts=['speedups.sh', '/tmp/s.sh'])

        runner._add_bootstrap_files_for_upload()

        self.assertIsNotNone(runner._master_bootstrap_script_path)
        self.assertTrue(os.path.exists(runner._master_bootstrap_script_path))

        with open(runner._master_bootstrap_script_path) as f:
            lines = [line.rstrip() for line in f]

        self.assertEqual(lines[0], '#!/bin/sh -ex')

        # check PWD gets stored
        self.assertIn('__mrjob_PWD=$PWD', lines)

        def assertScriptDownloads(path, name=None):
            uri = runner._upload_mgr.uri(path)
            name = runner._bootstrap_dir_mgr.name('file', path, name=name)

            self.assertIn(
                'hadoop fs -copyToLocal %s $__mrjob_PWD/%s' % (uri, name),
                lines)
            self.assertIn(
                'chmod a+x $__mrjob_PWD/%s' % (name,),
                lines)

        # check files get downloaded
        assertScriptDownloads(foo_py_path, 'bar.py')
        assertScriptDownloads('s3://walrus/scripts/ohnoes.sh')
        assertScriptDownloads('/tmp/quz', 'quz')
        assertScriptDownloads(runner._mrjob_tar_gz_path)
        assertScriptDownloads(yelpy_tar_gz_path)
        assertScriptDownloads('speedups.sh')
        assertScriptDownloads('/tmp/s.sh')

        # check scripts get run

        # bootstrap
        self.assertIn(PYTHON_BIN + ' $__mrjob_PWD/bar.py', lines)
        self.assertIn('$__mrjob_PWD/ohnoes.sh', lines)
        # bootstrap_cmds
        self.assertIn('echo "Hi!"', lines)
        self.assertIn('true', lines)
        self.assertIn('ls', lines)
        # bootstrap_mrjob
        mrjob_tar_gz_name = runner._bootstrap_dir_mgr.name(
            'file', runner._mrjob_tar_gz_path)
        self.assertIn("__mrjob_PYTHON_LIB=$(" + PYTHON_BIN + " -c 'from"
                      " distutils.sysconfig import get_python_lib;"
                      " print(get_python_lib())')", lines)
        self.assertIn('sudo tar xfz $__mrjob_PWD/' + mrjob_tar_gz_name +
                      ' -C $__mrjob_PYTHON_LIB', lines)
        self.assertIn('sudo ' + PYTHON_BIN + ' -m compileall -f'
                      ' $__mrjob_PYTHON_LIB/mrjob && true', lines)
        # bootstrap_python_packages
        self.assertIn('sudo apt-get install -y python-pip || '
                'sudo yum install -y python-pip', lines)
        self.assertIn('sudo pip install $__mrjob_PWD/yelpy.tar.gz', lines)
        # bootstrap_scripts
        self.assertIn('$__mrjob_PWD/speedups.sh', lines)
        self.assertIn('$__mrjob_PWD/s.sh', lines)

    def test_no_bootstrap_script_if_not_needed(self):
        runner = EMRJobRunner(conf_paths=[], bootstrap_mrjob=False,
                              bootstrap_python=False)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

        # bootstrap actions don't figure into the master bootstrap script
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_mrjob=False,
                              bootstrap_actions=['foo', 'bar baz'],
                              bootstrap_python=False,
                              pool_emr_job_flows=False)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

        # using pooling doesn't require us to create a bootstrap script
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_mrjob=False,
                              bootstrap_python=False,
                              pool_emr_job_flows=True)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

    def test_bootstrap_actions_get_added(self):
        bootstrap_actions = [
            ('s3://elasticmapreduce/bootstrap-actions/configure-hadoop'
             ' -m,mapred.tasktracker.map.tasks.maximum=1'),
            's3://foo/bar',
        ]

        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_actions=bootstrap_actions,
                              s3_sync_wait_time=0.00)

        cluster_id = runner.make_persistent_cluster()

        emr_conn = runner.make_emr_conn()
        actions = list(_yield_all_bootstrap_actions(emr_conn, cluster_id))

        self.assertEqual(len(actions), 3)

        self.assertEqual(
            actions[0].scriptpath,
            's3://elasticmapreduce/bootstrap-actions/configure-hadoop')
        self.assertEqual(
            actions[0].args[0].value,
            '-m,mapred.tasktracker.map.tasks.maximum=1')
        self.assertEqual(actions[0].name, 'action 0')

        self.assertEqual(actions[1].scriptpath, 's3://foo/bar')
        self.assertEqual(actions[1].args, [])
        self.assertEqual(actions[1].name, 'action 1')

        # check for master bootstrap script
        self.assertTrue(actions[2].scriptpath.startswith('s3://mrjob-'))
        self.assertTrue(actions[2].scriptpath.endswith('b.py'))
        self.assertEqual(actions[2].args, [])
        self.assertEqual(actions[2].name, 'master')

        # make sure master bootstrap script is on S3
        self.assertTrue(runner.path_exists(actions[2].scriptpath))

    def test_bootstrap_mrjob_uses_python_bin(self):
        # use all the bootstrap options
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_mrjob=True,
                              python_bin=['anaconda'])

        runner._add_bootstrap_files_for_upload()
        self.assertIsNotNone(runner._master_bootstrap_script_path)
        with open(runner._master_bootstrap_script_path, 'r') as f:
            content = f.read()

        self.assertIn('sudo anaconda -m compileall -f', content)

    def test_local_bootstrap_action(self):
        # make sure that local bootstrap action scripts get uploaded to S3
        action_path = os.path.join(self.tmp_dir, 'apt-install.sh')
        with open(action_path, 'w') as f:
            f.write('for $pkg in $@; do sudo apt-get install $pkg; done\n')

        bootstrap_actions = [
            action_path + ' python-scipy mysql-server']

        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_actions=bootstrap_actions,
                              s3_sync_wait_time=0.00)

        cluster_id = runner.make_persistent_cluster()

        emr_conn = runner.make_emr_conn()
        actions = list(_yield_all_bootstrap_actions(emr_conn, cluster_id))

        self.assertEqual(len(actions), 2)

        self.assertTrue(actions[0].scriptpath.startswith('s3://mrjob-'))
        self.assertTrue(actions[0].scriptpath.endswith('/apt-install.sh'))
        self.assertEqual(actions[0].name, 'action 0')
        self.assertEqual(actions[0].args[0].value, 'python-scipy')
        self.assertEqual(actions[0].args[1].value, 'mysql-server')

        # check for master boostrap script
        self.assertTrue(actions[1].scriptpath.startswith('s3://mrjob-'))
        self.assertTrue(actions[1].scriptpath.endswith('b.py'))
        self.assertEqual(actions[1].args, [])
        self.assertEqual(actions[1].name, 'master')

        # make sure master bootstrap script is on S3
        self.assertTrue(runner.path_exists(actions[1].scriptpath))


class EMRNoMapperTest(MockBotoTestCase):

    def setUp(self):
        super(EMRNoMapperTest, self).setUp()
        self.make_tmp_dir()

    def tearDown(self):
        super(EMRNoMapperTest, self).tearDown()
        self.rm_tmp_dir()

    def make_tmp_dir(self):
        self.tmp_dir = tempfile.mkdtemp()

    def rm_tmp_dir(self):
        shutil.rmtree(self.tmp_dir)

    def test_no_mapper(self):
        # read from STDIN, a local file, and a remote file
        stdin = BytesIO(b'foo\nbar\n')

        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as local_input_file:
            local_input_file.write('bar\nqux\n')

        remote_input_path = 's3://walrus/data/foo'
        self.add_mock_s3_data({'walrus': {'data/foo': b'foo\n'}})

        # setup fake output
        self.mock_emr_output = {('j-MOCKCLUSTER0', 1): [
            b'1\t"qux"\n2\t"bar"\n', b'2\t"foo"\n5\tnull\n']}

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '-', local_input_path, remote_input_path])
        mr_job.sandbox(stdin=stdin)

        results = []

        with mr_job.make_runner() as runner:
            runner.run()

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

        self.assertEqual(sorted(results),
                         [(1, 'qux'), (2, 'bar'), (2, 'foo'), (5, None)])


class PoolMatchingTestCase(MockBotoTestCase):

    def make_pooled_cluster(self, name=None, minutes_ago=0, **kwargs):
        """Returns ``(runner, cluster_id)``. Set minutes_ago to set
        ``jobflow.startdatetime`` to seconds before
        ``datetime.datetime.now()``."""
        runner = EMRJobRunner(pool_emr_job_flows=True,
                              emr_job_flow_pool_name=name,
                              **kwargs)
        cluster_id = runner.make_persistent_cluster()
        mock_cluster = self.mock_emr_clusters[cluster_id]

        mock_cluster.status.state = 'WAITING'
        start = datetime.now() - timedelta(minutes=minutes_ago)
        mock_cluster.status.timeline.creationdatetime = (
            start.strftime(boto.utils.ISO8601))
        return runner, cluster_id

    def get_cluster(self, job_args, job_class=MRTwoStepJob):
        mr_job = job_class(job_args)
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            self.prepare_runner_for_ssh(runner)
            runner.run()

            return runner.get_cluster_id()

    def assertJoins(self, cluster_id, job_args, job_class=MRTwoStepJob):
        actual_cluster_id = self.get_cluster(job_args, job_class=job_class)

        self.assertEqual(actual_cluster_id, cluster_id)

    def assertDoesNotJoin(self, cluster_id, job_args, job_class=MRTwoStepJob):

        actual_cluster_id = self.get_cluster(job_args, job_class=job_class)

        self.assertNotEqual(actual_cluster_id, cluster_id)

        # terminate the job flow created by this assert, to avoid
        # very confusing behavior (see Issue #331)
        emr_conn = EMRJobRunner(conf_paths=[]).make_emr_conn()
        emr_conn.terminate_jobflow(actual_cluster_id)

    def make_simple_runner(self, pool_name):
        """Make an EMRJobRunner that is ready to try to find a pool to join"""
        mr_job = MRTwoStepJob([
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', pool_name])
        mr_job.sandbox()
        runner = mr_job.make_runner()
        self.prepare_runner_for_ssh(runner)
        runner._prepare_for_launch()
        return runner

    def test_make_new_pooled_cluster(self):
        mr_job = MRTwoStepJob(['-r', 'emr', '-v', '--pool-emr-job-flows'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            self.prepare_runner_for_ssh(runner)
            runner.run()

            # Make sure that the runner made a pooling-enabled job flow
            emr_conn = runner.make_emr_conn()
            bootstrap_actions = list(_yield_all_bootstrap_actions(
                emr_conn, runner.get_cluster_id()))

            jf_hash, jf_name = _pool_hash_and_name(bootstrap_actions)
            self.assertEqual(jf_hash, runner._pool_hash())
            self.assertEqual(jf_name, runner._opts['emr_job_flow_pool_name'])

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.status.state, 'WAITING')

    def test_join_pooled_cluster(self):
        _, cluster_id = self.make_pooled_cluster()

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows'])

    def test_join_named_pool(self):
        _, cluster_id = self.make_pooled_cluster('pool1')

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', 'pool1'])

    def test_join_anyway_if_i_say_so(self):
        _, cluster_id = self.make_pooled_cluster(ami_version='2.0')

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--emr-job-flow-id', cluster_id,
            '--ami-version', '2.2'])

    def test_pooling_with_ami_version(self):
        _, cluster_id = self.make_pooled_cluster(ami_version='2.0')

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ami-version', '2.0'])

    def test_pooling_with_ami_version_prefix_major_minor(self):
        _, cluster_id = self.make_pooled_cluster(ami_version='2.0.0')

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ami-version', '2.0'])

    def test_pooling_with_ami_version_prefix_major(self):
        _, cluster_id = self.make_pooled_cluster(ami_version='2.0.0')

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ami-version', '2'])

    def test_dont_join_pool_with_wrong_ami_version(self):
        _, cluster_id = self.make_pooled_cluster(ami_version='2.2')

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ami-version', '2.0'])

    def test_pooling_with_additional_emr_info(self):
        info = '{"tomatoes": "actually a fruit!"}'
        _, cluster_id = self.make_pooled_cluster(
            additional_emr_info=info)

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--additional-emr-info', info])

    def test_dont_join_pool_with_wrong_additional_emr_info(self):
        info = '{"tomatoes": "actually a fruit!"}'
        _, cluster_id = self.make_pooled_cluster()

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--additional-emr-info', info])

    def test_join_pool_with_same_instance_type_and_count(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='m2.4xlarge',
            num_ec2_instances=20)

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm2.4xlarge',
            '--num-ec2-instances', '20'])

    def test_join_pool_with_more_of_same_instance_type(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='m2.4xlarge',
            num_ec2_instances=20)

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm2.4xlarge',
            '--num-ec2-instances', '5'])

    def test_join_cluster_with_bigger_instances(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='m2.4xlarge',
            num_ec2_instances=20)

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm1.medium',
            '--num-ec2-instances', '20'])

    def test_join_cluster_with_enough_cpu_and_memory(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='c1.xlarge',
            num_ec2_instances=3)

        # join the pooled job flow even though it has less instances total,
        # since they're have enough memory and CPU
        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm1.medium',
            '--num-ec2-instances', '10'])

    def test_dont_join_cluster_with_instances_with_too_little_memory(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='c1.xlarge',
            num_ec2_instances=20)

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm2.4xlarge',
            '--num-ec2-instances', '2'])

    def test_master_instance_has_to_be_big_enough(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='c1.xlarge',
            num_ec2_instances=10)

        # We implicitly want a MASTER instance with c1.xlarge. The pooled
        # job flow has an m1.medium master instance and 9 c1.xlarge core
        # instances, which doesn't match.
        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'c1.xlarge',
            '--num-ec2-instances', '1'])

    def test_unknown_instance_type_against_matching_pool(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='a1.sauce',
            num_ec2_instances=10)

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'a1.sauce',
            '--num-ec2-instances', '10'])

    def test_unknown_instance_type_against_pool_with_more_instances(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='a1.sauce',
            num_ec2_instances=20)

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'a1.sauce',
            '--num-ec2-instances', '10'])

    def test_unknown_instance_type_against_pool_with_less_instances(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='a1.sauce',
            num_ec2_instances=5)

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'a1.sauce',
            '--num-ec2-instances', '10'])

    def test_unknown_instance_type_against_other_instance_types(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_instance_type='m2.4xlarge',
            num_ec2_instances=100)

        # for all we know, "a1.sauce" instances have even more memory and CPU
        # than m2.4xlarge
        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'a1.sauce',
            '--num-ec2-instances', '2'])

    def test_can_join_cluster_with_same_bid_price(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_master_instance_bid_price='0.25')

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-master-instance-bid-price', '0.25'])

    def test_can_join_cluster_with_higher_bid_price(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_master_instance_bid_price='25.00')

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-master-instance-bid-price', '0.25'])

    def test_cant_join_cluster_with_lower_bid_price(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_master_instance_bid_price='0.25',
            num_ec2_instances=100)

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-master-instance-bid-price', '25.00'])

    def test_on_demand_satisfies_any_bid_price(self):
        _, cluster_id = self.make_pooled_cluster()

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-master-instance-bid-price', '25.00'])

    def test_no_bid_price_satisfies_on_demand(self):
        _, cluster_id = self.make_pooled_cluster(
            ec2_master_instance_bid_price='25.00')

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows'])

    def test_core_and_task_instance_types(self):
        # a tricky test that mixes and matches different criteria
        _, cluster_id = self.make_pooled_cluster(
            ec2_core_instance_bid_price='0.25',
            ec2_task_instance_bid_price='25.00',
            ec2_task_instance_type='c1.xlarge',
            num_ec2_core_instances=2,
            num_ec2_task_instances=3)

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--num-ec2-core-instances', '2',
            '--num-ec2-task-instances', '10',  # more instances, but smaller
            '--ec2-core-instance-bid-price', '0.10',
            '--ec2-master-instance-bid-price', '77.77',
            '--ec2-task-instance-bid-price', '22.00'])

    def test_dont_join_full_cluster(self):
        dummy_runner, cluster_id = self.make_pooled_cluster('pool1')

        # fill the job flow
        self.mock_emr_clusters[cluster_id]._steps = 255 * [
            MockEmrObject(
                actiononfailure='CANCEL_AND_WAIT',
                config=MockEmrObject(args=[]),
                name='dummy',
                status=MockEmrObject(
                    state='COMPLETED',
                    timeline=MockEmrObject(
                        enddatetime='definitely not none')))
        ]

        # a two-step job shouldn't fit
        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', 'pool1'],
            job_class=MRTwoStepJob)

    def test_join_almost_full_cluster(self):
        dummy_runner, cluster_id = self.make_pooled_cluster('pool1')

        # fill the job flow
        self.mock_emr_clusters[cluster_id]._steps = 255 * [
            MockEmrObject(
                actiononfailure='CANCEL_AND_WAIT',
                config=MockEmrObject(args=[]),
                name='dummy',
                status=MockEmrObject(
                    state='COMPLETED',
                    timeline=MockEmrObject(
                        enddatetime='definitely not none')))
        ]

        # a one-step job should fit
        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', 'pool1'],
            job_class=MRWordCount)

    def test_dont_join_idle_with_pending_steps(self):
        dummy_runner, cluster_id = self.make_pooled_cluster()

        self.mock_emr_clusters[cluster_id]._steps = [
            MockEmrObject(
                actiononfailure='CANCEL_AND_WAIT',
                config=MockEmrObject(args=[]),
                mock_no_progress=True,
                name='dummy',
                status=MockEmrObject(state='PENDING'))]

        self.assertDoesNotJoin(cluster_id,
                               ['-r', 'emr', '--pool-emr-job-flows'])

    def test_do_join_idle_with_cancelled_steps(self):
        dummy_runner, cluster_id = self.make_pooled_cluster()

        self.mock_emr_clusters[cluster_id].steps = [
            MockEmrObject(
                state='FAILED',
                name='step 1 of 2',
                actiononfailure='CANCEL_AND_WAIT',
                enddatetime='sometime in the past',
                args=[]),
            # step 2 never ran, so its enddatetime is not set
            MockEmrObject(
                state='CANCELLED',
                name='step 2 of 2',
                actiononfailure='CANCEL_AND_WAIT',
                args=[])
        ]

        self.assertJoins(cluster_id,
                         ['-r', 'emr', '--pool-emr-job-flows'])

    def test_dont_join_wrong_named_pool(self):
        _, cluster_id = self.make_pooled_cluster('pool1')

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', 'not_pool1'])

    def test_dont_join_wrong_mrjob_version(self):
        _, cluster_id = self.make_pooled_cluster('pool1')

        old_version = mrjob.__version__

        try:
            mrjob.__version__ = 'OVER NINE THOUSAAAAAND'

            self.assertDoesNotJoin(cluster_id, [
                '-r', 'emr', '-v', '--pool-emr-job-flows',
                '--pool-name', 'not_pool1'])
        finally:
            mrjob.__version__ = old_version

    def test_join_similarly_bootstrapped_pool(self):
        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as input_file:
            input_file.write('bar\nfoo\n')

        _, cluster_id = self.make_pooled_cluster(
            bootstrap_files=[local_input_path])

        self.assertJoins(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--bootstrap-file', local_input_path])

    def test_dont_join_differently_bootstrapped_pool(self):
        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as input_file:
            input_file.write('bar\nfoo\n')

        _, cluster_id = self.make_pooled_cluster()

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--bootstrap-file', local_input_path])

    def test_dont_join_differently_bootstrapped_pool_2(self):
        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as input_file:
            input_file.write('bar\nfoo\n')

        bootstrap_path = os.path.join(self.tmp_dir, 'go.sh')
        with open(bootstrap_path, 'w') as f:
            f.write('#!/usr/bin/sh\necho "hi mom"\n')

        _, cluster_id = self.make_pooled_cluster()

        self.assertDoesNotJoin(cluster_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--bootstrap-action', bootstrap_path + ' a b c'])

    def test_pool_contention(self):
        _, cluster_id = self.make_pooled_cluster('robert_downey_jr')

        def runner_plz():
            mr_job = MRTwoStepJob([
                '-r', 'emr', '-v', '--pool-emr-job-flows',
                '--pool-name', 'robert_downey_jr'])
            mr_job.sandbox()
            runner = mr_job.make_runner()
            runner._prepare_for_launch()
            return runner

        runner_1 = runner_plz()
        runner_2 = runner_plz()

        self.assertEqual(runner_1._find_cluster(), cluster_id)
        self.assertEqual(runner_2._find_cluster(), None)

    def test_sorting_by_time(self):
        _, cluster_id_1 = self.make_pooled_cluster('pool1', minutes_ago=20)
        _, cluster_id_2 = self.make_pooled_cluster('pool1', minutes_ago=40)

        runner_1 = self.make_simple_runner('pool1')
        runner_2 = self.make_simple_runner('pool1')

        self.assertEqual(runner_1._find_cluster(), cluster_id_1)
        self.assertEqual(runner_2._find_cluster(), cluster_id_2)

    def test_sorting_by_cpu_hours(self):
        _, cluster_id_1 = self.make_pooled_cluster('pool1',
                                                     minutes_ago=40,
                                                     num_ec2_instances=2)
        _, cluster_id_2 = self.make_pooled_cluster('pool1',
                                                     minutes_ago=20,
                                                     num_ec2_instances=1)

        runner_1 = self.make_simple_runner('pool1')
        runner_2 = self.make_simple_runner('pool1')

        self.assertEqual(runner_1._find_cluster(), cluster_id_1)
        self.assertEqual(runner_2._find_cluster(), cluster_id_2)

    def test_dont_destroy_own_pooled_cluster_on_failure(self):
        # Issue 242: job failure shouldn't kill the pooled job flows
        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--pool-emr-job-flow'])
        mr_job.sandbox()

        self.mock_emr_failures = {('j-MOCKCLUSTER0', 0): None}

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, EMRJobRunner)
            self.prepare_runner_for_ssh(runner)
            with logger_disabled('mrjob.emr'):
                self.assertRaises(Exception, runner.run)

            emr_conn = runner.make_emr_conn()
            cluster_id = runner.get_cluster_id()
            for _ in range(10):
                emr_conn.simulate_progress(cluster_id)

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.status.state, 'WAITING')

        # job shouldn't get terminated by cleanup
        emr_conn = runner.make_emr_conn()
        cluster_id = runner.get_cluster_id()
        for _ in range(10):
            emr_conn.simulate_progress(cluster_id)

        cluster = runner._describe_cluster()
        self.assertEqual(cluster.status.state, 'WAITING')

    def test_dont_destroy_other_pooled_cluster_on_failure(self):
        # Issue 242: job failure shouldn't kill the pooled job flows
        _, cluster_id = self.make_pooled_cluster()

        self.mock_emr_failures = {(cluster_id, 0): None}

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--pool-emr-job-flow'])
        mr_job.sandbox()

        self.mock_emr_failures = set([('j-MOCKCLUSTER0', 0)])

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, EMRJobRunner)
            self.prepare_runner_for_ssh(runner)
            with logger_disabled('mrjob.emr'):
                self.assertRaises(Exception, runner.run)

            self.assertEqual(runner.get_cluster_id(), cluster_id)

            emr_conn = runner.make_emr_conn()
            for _ in range(10):
                emr_conn.simulate_progress(cluster_id)

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.status.state, 'WAITING')

        # job shouldn't get terminated by cleanup
        emr_conn = runner.make_emr_conn()
        cluster_id = runner.get_cluster_id()
        for _ in range(10):
            emr_conn.simulate_progress(cluster_id)

        cluster = runner._describe_cluster()
        self.assertEqual(cluster.status.state, 'WAITING')

    def test_max_hours_idle_doesnt_affect_pool_hash(self):
        # max_hours_idle uses a bootstrap action, but it's not included
        # in the pool hash
        _, cluster_id = self.make_pooled_cluster()

        self.assertJoins(cluster_id, [
            '-r', 'emr', '--pool-emr-job-flows', '--max-hours-idle', '1'])

    def test_can_join_cluster_started_with_max_hours_idle(self):
        _, cluster_id = self.make_pooled_cluster(max_hours_idle=1)

        self.assertJoins(cluster_id, ['-r', 'emr', '--pool-emr-job-flows'])


class PoolingDisablingTestCase(MockBotoTestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'emr': {
        'check_emr_status_every': 0.00,
        's3_sync_wait_time': 0.00,
        'pool_emr_job_flows': True,
    }}}

    def test_can_turn_off_pooling_from_cmd_line(self):
        mr_job = MRTwoStepJob(['-r', 'emr', '-v', '--no-pool-emr-job-flows'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            self.prepare_runner_for_ssh(runner)
            runner.run()

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.autoterminate, 'true')


class S3LockTestCase(MockBotoTestCase):

    def setUp(self):
        super(S3LockTestCase, self).setUp()
        self.make_buckets()

    def make_buckets(self):
        self.add_mock_s3_data({'locks': {
            'expired_lock': b'x',
        }}, datetime.utcnow() - timedelta(minutes=30))
        self.lock_uri = 's3://locks/some_lock'
        self.expired_lock_uri = 's3://locks/expired_lock'

    def test_lock(self):
        # Most basic test case
        runner = EMRJobRunner(conf_paths=[])

        self.assertEqual(
            True, attempt_to_acquire_lock(runner.fs, self.lock_uri, 0, 'jf1'))

        self.assertEqual(
            False, attempt_to_acquire_lock(runner.fs, self.lock_uri, 0, 'jf2'))

    def test_lock_expiration(self):
        runner = EMRJobRunner(conf_paths=[])

        did_lock = attempt_to_acquire_lock(
            runner.fs, self.expired_lock_uri, 0, 'jf1',
            mins_to_expiration=5)
        self.assertEqual(True, did_lock)

    def test_key_race_condition(self):
        # Test case where one attempt puts the key in existence
        runner = EMRJobRunner(conf_paths=[])

        key = _lock_acquire_step_1(runner.fs, self.lock_uri, 'jf1')
        self.assertNotEqual(key, None)

        key2 = _lock_acquire_step_1(runner.fs, self.lock_uri, 'jf2')
        self.assertEqual(key2, None)

    def test_read_race_condition(self):
        # test case where both try to create the key
        runner = EMRJobRunner(conf_paths=[])

        key = _lock_acquire_step_1(runner.fs, self.lock_uri, 'jf1')
        self.assertNotEqual(key, None)

        # acquire the key by subversive means to simulate contention
        bucket_name, key_prefix = parse_s3_uri(self.lock_uri)
        bucket = runner.fs.get_bucket(bucket_name)
        key2 = bucket.get_key(key_prefix)

        # and take the lock!
        key2.set_contents_from_string(b'jf2')

        self.assertFalse(_lock_acquire_step_2(key, 'jf1'), 'Lock should fail')


class MaxHoursIdleTestCase(MockBotoTestCase):

    def assertRanIdleTimeoutScriptWith(self, runner, args):
        emr_conn = runner.make_emr_conn()
        cluster_id = runner.get_cluster_id()

        actions = list(_yield_all_bootstrap_actions(emr_conn, cluster_id))
        action = actions[-1]

        self.assertEqual(action.name, 'idle timeout')
        self.assertEqual(
            action.scriptpath,
            runner._upload_mgr.uri(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH))
        self.assertEqual([arg.value for arg in action.args], args)

    def assertDidNotUseIdleTimeoutScript(self, runner):
        emr_conn = runner.make_emr_conn()
        cluster_id = runner.get_cluster_id()

        actions = list(_yield_all_bootstrap_actions(emr_conn, cluster_id))
        action_names = [a.name for a in actions]

        self.assertNotIn('idle timeout', action_names)
        # idle timeout script should not even be uploaded
        self.assertNotIn(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH,
                         runner._upload_mgr.path_to_uri())

    def test_default(self):
        mr_job = MRWordCount(['-r', 'emr'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_non_persistent_job_flow(self):
        mr_job = MRWordCount(['-r', 'emr', '--max-hours-idle', '1'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_persistent_job_flow(self):
        mr_job = MRWordCount(['-r', 'emr', '--max-hours-idle', '0.01'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_job_flow()
            self.assertRanIdleTimeoutScriptWith(runner, ['36', '300'])

    def test_mins_to_end_of_hour(self):
        mr_job = MRWordCount(['-r', 'emr', '--max-hours-idle', '1',
                              '--mins-to-end-of-hour', '10'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_job_flow()
            self.assertRanIdleTimeoutScriptWith(runner, ['3600', '600'])

    def test_mins_to_end_of_hour_does_nothing_without_max_hours_idle(self):
        mr_job = MRWordCount(['-r', 'emr', '--mins-to-end-of-hour', '10'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_job_flow()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_use_integers(self):
        mr_job = MRWordCount(['-r', 'emr', '--max-hours-idle', '1.000001',
                              '--mins-to-end-of-hour', '10.000001'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_job_flow()
            self.assertRanIdleTimeoutScriptWith(runner, ['3600', '600'])

    def pooled_job_flows(self):
        mr_job = MRWordCount(['-r', 'emr', '--pool-emr-job-flows',
                              '--max-hours-idle', '0.5'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertRanIdleTimeoutScriptWith(runner, ['1800', '300'])

    def test_bootstrap_script_is_actually_installed(self):
        self.assertTrue(os.path.exists(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH))

class TestCatFallback(MockBotoTestCase):

    def test_s3_cat(self):
        self.add_mock_s3_data(
            {'walrus': {'one': b'one_text',
                        'two': b'two_text',
                        'three': b'three_text'}})

        runner = EMRJobRunner(s3_scratch_uri='s3://walrus/tmp',
                              conf_paths=[])

        self.assertEqual(list(runner.cat('s3://walrus/one')), [b'one_text'])

    def test_ssh_cat(self):
        runner = EMRJobRunner(conf_paths=[])
        self.prepare_runner_for_ssh(runner)
        mock_ssh_file('testmaster', 'etc/init.d', b'meow')

        ssh_cat_gen = runner.cat(
            SSH_PREFIX + runner._address + '/etc/init.d')
        self.assertEqual(list(ssh_cat_gen)[0].rstrip(), b'meow')
        self.assertRaises(
            IOError, list,
            runner.cat(SSH_PREFIX + runner._address + '/does_not_exist'))

    def test_ssh_cat_errlog(self):
        # A file *containing* an error message shouldn't cause an error.
        runner = EMRJobRunner(conf_paths=[])
        self.prepare_runner_for_ssh(runner)

        error_message = b'cat: logs/err.log: No such file or directory\n'
        mock_ssh_file('testmaster', 'logs/err.log', error_message)
        self.assertEqual(
            list(runner.cat(SSH_PREFIX + runner._address + '/logs/err.log')),
            [error_message])


class CleanUpJobTestCase(MockBotoTestCase):

    @contextmanager
    def _test_mode(self, mode):
        r = EMRJobRunner(conf_paths=[])
        with patch.multiple(r,
                            _cleanup_job=mock.DEFAULT,
                            _cleanup_job_flow=mock.DEFAULT,
                            _cleanup_local_scratch=mock.DEFAULT,
                            _cleanup_logs=mock.DEFAULT,
                            _cleanup_remote_scratch=mock.DEFAULT) as mock_dict:
            r.cleanup(mode=mode)
            yield mock_dict

    def _quick_runner(self):
        r = EMRJobRunner(conf_paths=[], ec2_key_pair_file='fake.pem')
        r._cluster_id = 'j-ESSEOWENS'
        r._address = 'Albuquerque, NM'
        r._ran_job = False
        return r

    def test_cleanup_all(self):
        with self._test_mode('ALL') as m:
            self.assertFalse(m['_cleanup_job_flow'].called)
            self.assertFalse(m['_cleanup_job'].called)
            self.assertTrue(m['_cleanup_local_scratch'].called)
            self.assertTrue(m['_cleanup_remote_scratch'].called)
            self.assertTrue(m['_cleanup_logs'].called)

    def test_cleanup_job(self):
        with self._test_mode('JOB') as m:
            self.assertFalse(m['_cleanup_local_scratch'].called)
            self.assertFalse(m['_cleanup_remote_scratch'].called)
            self.assertFalse(m['_cleanup_logs'].called)
            self.assertFalse(m['_cleanup_job_flow'].called)
            self.assertFalse(m['_cleanup_job'].called)  # Only on failure

    def test_cleanup_none(self):
        with self._test_mode('NONE') as m:
            self.assertFalse(m['_cleanup_local_scratch'].called)
            self.assertFalse(m['_cleanup_remote_scratch'].called)
            self.assertFalse(m['_cleanup_logs'].called)
            self.assertFalse(m['_cleanup_job'].called)
            self.assertFalse(m['_cleanup_job_flow'].called)

    def test_job_cleanup_mechanics_succeed(self):
        with no_handlers_for_logger():
            r = self._quick_runner()
            with patch.object(mrjob.emr, 'ssh_terminate_single_job') as m:
                r._cleanup_job()
            self.assertTrue(m.called)
            m.assert_any_call(['ssh'], 'Albuquerque, NM', 'fake.pem')

    def test_job_cleanup_mechanics_ssh_fail(self):
        def die_ssh(*args, **kwargs):
            raise IOError

        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            stderr = StringIO()
            log_to_stream('mrjob.emr', stderr)
            with patch.object(mrjob.emr, 'ssh_terminate_single_job',
                              side_effect=die_ssh):
                r._cleanup_job()
                self.assertIn('Unable to kill job', stderr.getvalue())

    def test_job_cleanup_mechanics_io_fail(self):
        def die_io(*args, **kwargs):
            raise IOError

        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr, 'ssh_terminate_single_job',
                              side_effect=die_io):
                stderr = StringIO()
                log_to_stream('mrjob.emr', stderr)
                r._cleanup_job()
                self.assertIn('Unable to kill job', stderr.getvalue())

    def test_dont_kill_if_successful(self):
        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr, 'ssh_terminate_single_job') as m:
                r._ran_job = True
                r._cleanup_job()
                m.assert_not_called()

    def test_kill_job_flow(self):
        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr.EMRJobRunner, 'make_emr_conn') as m:
                r._cleanup_job_flow()
                self.assertTrue(m().terminate_jobflow.called)

    def test_kill_job_flow_if_successful(self):
        # If they are setting up the cleanup to kill the job flow, mrjob should
        # kill the job flow independent of job success.
        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr.EMRJobRunner, 'make_emr_conn') as m:
                r._ran_job = True
                r._cleanup_job_flow()
                self.assertTrue(m().terminate_jobflow.called)

    def test_kill_persistent_job_flow(self):
        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr.EMRJobRunner, 'make_emr_conn') as m:
                r._opts['emr_job_flow_id'] = 'j-MOCKCLUSTER0'
                r._cleanup_job_flow()
                self.assertTrue(m().terminate_jobflow.called)


class JobWaitTestCase(MockBotoTestCase):

    # A list of job ids that hold booleans of whether or not the job can
    # acquire a lock. Helps simulate mrjob.emr.attempt_to_acquire_lock.
    JOB_ID_LOCKS = {
        'j-fail-lock': False,
        'j-successful-lock': True,
        'j-brown': True,
        'j-epic-fail-lock': False
    }

    def setUp(self):
        super(JobWaitTestCase, self).setUp()
        self.future_mock_cluster_ids = []
        self.mock_cluster_ids = []
        self.sleep_counter = 0

        def side_effect_lock_uri(*args):
            return args[0]  # Return the only arg given to it.

        def side_effect_acquire_lock(*args):
            cluster_id = args[1]
            return self.JOB_ID_LOCKS[cluster_id]

        def side_effect_usable_clusters(*args, **kwargs):
            return [
                (cluster_id, 0) for cluster_id in self.mock_cluster_ids
                if cluster_id not in kwargs['exclude']]

        def side_effect_time_sleep(*args):
            self.sleep_counter += 1
            if self.future_mock_cluster_ids:
                cluster_id = self.future_mock_cluster_ids.pop(0)
                self.mock_cluster_ids.append(cluster_id)

        self.start(patch.object(EMRJobRunner, 'make_emr_conn'))
        self.start(patch.object(S3Filesystem, 'make_s3_conn',
                                side_effect=self.connect_s3))
        self.start(patch.object(EMRJobRunner, '_usable_clusters',
                                side_effect=side_effect_usable_clusters))
        self.start(patch.object(EMRJobRunner, '_lock_uri',
                                side_effect=side_effect_lock_uri))
        self.start(patch.object(mrjob.emr, 'attempt_to_acquire_lock',
                                side_effect=side_effect_acquire_lock))
        self.start(patch.object(time, 'sleep',
                                side_effect=side_effect_time_sleep))

    def tearDown(self):
        super(JobWaitTestCase, self).tearDown()
        self.mock_cluster_ids = []
        self.future_mock_cluster_ids = []

    def test_no_waiting_for_job_pool_fail(self):
        self.mock_cluster_ids.append('j-fail-lock')

        runner = EMRJobRunner(conf_paths=[], pool_wait_minutes=0)
        cluster_id = runner._find_cluster()

        self.assertEqual(cluster_id, None)
        self.assertEqual(self.sleep_counter, 0)

    def test_no_waiting_for_job_pool_success(self):
        self.mock_cluster_ids.append('j-fail-lock')
        runner = EMRJobRunner(conf_paths=[], pool_wait_minutes=0)
        cluster_id = runner._find_cluster()

        self.assertEqual(cluster_id, None)

    def test_acquire_lock_on_first_attempt(self):
        self.mock_cluster_ids.append('j-successful-lock')
        runner = EMRJobRunner(conf_paths=[], pool_wait_minutes=1)
        cluster_id = runner._find_cluster()

        self.assertEqual(cluster_id, 'j-successful-lock')
        self.assertEqual(self.sleep_counter, 0)

    def test_sleep_then_acquire_lock(self):
        self.mock_cluster_ids.append('j-fail-lock')
        self.future_mock_cluster_ids.append('j-successful-lock')
        runner = EMRJobRunner(conf_paths=[], pool_wait_minutes=1)
        cluster_id = runner._find_cluster()

        self.assertEqual(cluster_id, 'j-successful-lock')
        self.assertEqual(self.sleep_counter, 1)

    def test_timeout_waiting_for_job_flow(self):
        self.mock_cluster_ids.append('j-fail-lock')
        self.future_mock_cluster_ids.append('j-epic-fail-lock')
        runner = EMRJobRunner(conf_paths=[], pool_wait_minutes=1)
        cluster_id = runner._find_cluster()

        self.assertEqual(cluster_id, None)
        self.assertEqual(self.sleep_counter, 2)


class BuildStreamingStepTestCase(MockBotoTestCase):

    def setUp(self):
        super(BuildStreamingStepTestCase, self).setUp()
        with patch_fs_s3():
            self.runner = EMRJobRunner(
                mr_job_script='my_job.py', conf_paths=[], stdin=BytesIO())
        self.runner._steps = []  # don't actually run `my_job.py --steps`
        self.runner._add_job_files_for_upload()

        self.start(patch.object(
            self.runner, '_step_input_uris', return_value=['input']))
        self.start(patch.object(
            self.runner, '_step_output_uri', return_value=['output']))
        self.start(patch.object(
            self.runner, '_get_streaming_jar', return_value=['streaming.jar']))

        self.start(patch.object(boto.emr, 'StreamingStep', dict))
        self.runner._hadoop_version = '0.20'

    def _assert_streaming_step(self, step, **kwargs):
        self.runner._steps = [step]
        d = self.runner._build_streaming_step(0)
        for k, v in kwargs.items():
            self.assertEqual(d[k], v)

    def test_basic_mapper(self):
        self._assert_streaming_step(
            {
                'type': 'streaming',
                'mapper': {
                    'type': 'script',
                },
            },
            mapper=(PYTHON_BIN + ' my_job.py --step-num=0 --mapper'),
            reducer=None,
        )

    def test_basic_reducer(self):
        self._assert_streaming_step(
            {
                'type': 'streaming',
                'reducer': {
                    'type': 'script',
                },
            },
            mapper="cat",
            reducer=(PYTHON_BIN +
                     ' my_job.py --step-num=0 --reducer'),
        )

    def test_pre_filters(self):
        self._assert_streaming_step(
            {
                'type': 'streaming',
                'mapper': {
                    'type': 'script',
                    'pre_filter': 'grep anything',
                },
                'combiner': {
                    'type': 'script',
                    'pre_filter': 'grep nothing',
                },
                'reducer': {
                    'type': 'script',
                    'pre_filter': 'grep something',
                },
            },
            mapper=("bash -c 'grep anything | " +
                    PYTHON_BIN +
                    " my_job.py --step-num=0 --mapper'"),
            combiner=("bash -c 'grep nothing | " +
                      PYTHON_BIN +
                      " my_job.py --step-num=0 --combiner'"),
            reducer=("bash -c 'grep something | " +
                     PYTHON_BIN +
                     " my_job.py --step-num=0 --reducer'"),
        )

    def test_pre_filter_escaping(self):
        # ESCAPE ALL THE THINGS!!!
        self._assert_streaming_step(
            {
                'type': 'streaming',
                'mapper': {
                    'type': 'script',
                    'pre_filter': bash_wrap("grep 'anything'"),
                },
            },
            mapper=(
                "bash -c 'bash -c '\\''grep"
                " '\\''\\'\\'''\\''anything'\\''\\'\\'''\\'''\\'' | " +
                PYTHON_BIN +
                " my_job.py --step-num=0 --mapper'"),
        )


class JarStepTestCase(MockBotoTestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'emr': {
        'check_emr_status_every': 0.00,
        's3_sync_wait_time': 0.00,
    }}}

    def test_local_jar_gets_uploaded(self):
        fake_jar = os.path.join(self.tmp_dir, 'fake.jar')
        with open(fake_jar, 'w'):
            pass

        job = MRJustAJar(['-r', 'emr', '--jar', fake_jar])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()

            self.assertIn(fake_jar, runner._upload_mgr.path_to_uri())
            jar_uri = runner._upload_mgr.uri(fake_jar)
            self.assertTrue(runner.ls(jar_uri))

            emr_conn = runner.make_emr_conn()
            steps = list(_yield_all_steps(emr_conn, runner.get_cluster_id()))

            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0].config.jar, jar_uri)

    def test_jar_on_s3(self):
        self.add_mock_s3_data({'dubliners': {'whiskeyinthe.jar': b''}})
        JAR_URI = 's3://dubliners/whiskeyinthe.jar'

        job = MRJustAJar(['-r', 'emr', '--jar', JAR_URI])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()

            emr_conn = runner.make_emr_conn()
            steps = list(_yield_all_steps(emr_conn, runner.get_cluster_id()))

            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0].config.jar, JAR_URI)

    def test_jar_inside_emr(self):
        job = MRJustAJar(['-r', 'emr', '--jar',
                          'file:///home/hadoop/hadoop-examples.jar'])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()

            emr_conn = runner.make_emr_conn()
            steps = list(_yield_all_steps(emr_conn, runner.get_cluster_id()))

            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0].config.jar,
                             '/home/hadoop/hadoop-examples.jar')

    def test_input_output_interpolation(self):
        fake_jar = os.path.join(self.tmp_dir, 'fake.jar')
        open(fake_jar, 'w').close()
        input1 = os.path.join(self.tmp_dir, 'input1')
        open(input1, 'w').close()
        input2 = os.path.join(self.tmp_dir, 'input2')
        open(input2, 'w').close()

        job = MRJarAndStreaming(
            ['-r', 'emr', '--jar', fake_jar, input1, input2])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()

            emr_conn = runner.make_emr_conn()
            steps = list(_yield_all_steps(emr_conn, runner.get_cluster_id()))

            self.assertEqual(len(steps), 2)
            jar_step, streaming_step = steps

            # on EMR, the jar gets uploaded
            self.assertEqual(jar_step.config.jar,
                             runner._upload_mgr.uri(fake_jar))

            jar_args = [a.value for a in jar_step.config.args]
            self.assertEqual(len(jar_args), 3)
            self.assertEqual(jar_args[0], 'stuff')

            # check input is interpolated
            input_arg = ','.join(
                runner._upload_mgr.uri(path) for path in (input1, input2))
            self.assertEqual(jar_args[1], input_arg)

            # check output of jar is input of next step
            jar_output_arg = jar_args[2]

            streaming_args = [a.value for a in streaming_step.config.args]
            streaming_input_arg = streaming_args[
                streaming_args.index('-input') + 1]
            self.assertEqual(jar_output_arg, streaming_input_arg)


class ActionOnFailureTestCase(MockBotoTestCase):

    def test_default(self):
        runner = EMRJobRunner()
        self.assertEqual(runner._action_on_failure,
                         'TERMINATE_CLUSTER')

    def test_default_with_job_flow_id(self):
        runner = EMRJobRunner(emr_job_flow_id='j-JOBFLOW')
        self.assertEqual(runner._action_on_failure,
                         'CANCEL_AND_WAIT')

    def test_default_with_pooling(self):
        runner = EMRJobRunner(pool_emr_job_flows=True)
        self.assertEqual(runner._action_on_failure,
                         'CANCEL_AND_WAIT')

    def test_option(self):
        runner = EMRJobRunner(emr_action_on_failure='CONTINUE')
        self.assertEqual(runner._action_on_failure,
                         'CONTINUE')

    def test_switch(self):
        mr_job = MRWordCount(
            ['-r', 'emr', '--emr-action-on-failure', 'CONTINUE'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            self.assertEqual(runner._action_on_failure, 'CONTINUE')


class MultiPartUploadTestCase(MockBotoTestCase):

    PART_SIZE_IN_MB = 50.0 / 1024 / 1024
    TEST_BUCKET = 'walrus'
    TEST_FILENAME = 'data.dat'
    TEST_S3_URI = 's3://%s/%s' % (TEST_BUCKET, TEST_FILENAME)

    def setUp(self):
        super(MultiPartUploadTestCase, self).setUp()
        # create the walrus bucket
        self.add_mock_s3_data({self.TEST_BUCKET: {}})

    def upload_data(self, runner, data):
        """Upload some bytes to S3"""
        data_path = os.path.join(self.tmp_dir, self.TEST_FILENAME)
        with open(data_path, 'wb') as fp:
            fp.write(data)

        runner._upload_contents(self.TEST_S3_URI, data_path)

    def assert_upload_succeeds(self, runner, data, expect_multipart):
        """Write the data to a temp file, and then upload it to (mock) S3,
        checking that the data successfully uploaded."""
        with patch.object(runner, '_upload_parts', wraps=runner._upload_parts):
            self.upload_data(runner, data)

            s3_key = runner.get_s3_key(self.TEST_S3_URI)
            self.assertEqual(s3_key.get_contents_as_string(), data)
            self.assertEqual(runner._upload_parts.called, expect_multipart)

    def test_small_file(self):
        runner = EMRJobRunner()
        data = b'beavers mate for life'

        self.assert_upload_succeeds(runner, data, expect_multipart=False)

    @skipIf(filechunkio is None, 'need filechunkio')
    def test_large_file(self):
        # Real S3 has a minimum chunk size of 5MB, but I'd rather not
        # store that in memory (in our mock S3 filesystem)
        runner = EMRJobRunner(s3_upload_part_size=self.PART_SIZE_IN_MB)
        self.assertEqual(runner._get_upload_part_size(), 50)

        data = b'Mew' * 20
        self.assert_upload_succeeds(runner, data, expect_multipart=True)

    def test_file_size_equals_part_size(self):
        runner = EMRJobRunner(s3_upload_part_size=self.PART_SIZE_IN_MB)
        self.assertEqual(runner._get_upload_part_size(), 50)

        data = b'o' * 50
        self.assert_upload_succeeds(runner, data, expect_multipart=False)

    def test_disable_multipart(self):
        runner = EMRJobRunner(s3_upload_part_size=0)
        self.assertEqual(runner._get_upload_part_size(), 0)

        data = b'Mew' * 20
        self.assert_upload_succeeds(runner, data, expect_multipart=False)

    def test_no_filechunkio(self):
        with patch.object(mrjob.emr, 'filechunkio', None):
            runner = EMRJobRunner(s3_upload_part_size=self.PART_SIZE_IN_MB)
            self.assertEqual(runner._get_upload_part_size(), 50)

            data = b'Mew' * 20
            with logger_disabled('mrjob.emr'):
                self.assert_upload_succeeds(runner, data,
                                            expect_multipart=False)

    @skipIf(filechunkio is None, 'need filechunkio')
    def test_exception_while_uploading_large_file(self):

        runner = EMRJobRunner(s3_upload_part_size=self.PART_SIZE_IN_MB)
        self.assertEqual(runner._get_upload_part_size(), 50)

        data = b'Mew' * 20

        with patch.object(runner, '_upload_parts', side_effect=IOError):
            self.assertRaises(IOError, self.upload_data, runner, data)

            s3_key = runner.get_s3_key(self.TEST_S3_URI)
            self.assertTrue(s3_key.mock_multipart_upload_was_cancelled())


class SecurityTokenTestCase(MockBotoTestCase):

    def setUp(self):
        super(SecurityTokenTestCase, self).setUp()

        self.mock_emr = self.start(patch('boto.emr.connection.EmrConnection'))
        self.mock_iam = self.start(patch('boto.connect_iam'))

        # runner needs to do stuff with S3 on initialization
        self.mock_s3 = self.start(patch('boto.connect_s3',
                                        wraps=boto.connect_s3))

    def assert_conns_use_security_token(self, runner, security_token):
        runner.make_emr_conn()

        self.assertTrue(self.mock_emr.called)
        emr_kwargs = self.mock_emr.call_args[1]
        self.assertIn('security_token', emr_kwargs)
        self.assertEqual(emr_kwargs['security_token'], security_token)

        runner.make_iam_conn()

        self.assertTrue(self.mock_iam.called)
        iam_kwargs = self.mock_iam.call_args[1]
        self.assertIn('security_token', iam_kwargs)
        self.assertEqual(iam_kwargs['security_token'], security_token)

        runner.make_s3_conn()

        self.assertTrue(self.mock_s3.called)
        s3_kwargs = self.mock_s3.call_args[1]
        self.assertIn('security_token', s3_kwargs)
        self.assertEqual(s3_kwargs['security_token'], security_token)

    def test_connections_without_security_token(self):
        runner = EMRJobRunner()

        self.assert_conns_use_security_token(runner, None)

    def test_connections_with_security_token(self):
        runner = EMRJobRunner(aws_security_token='meow')

        self.assert_conns_use_security_token(runner, 'meow')


class BootstrapPythonTestCase(MockBotoTestCase):

    def test_default(self):
        mr_job = MRTwoStepJob(['-r', 'emr'])
        with mr_job.make_runner() as runner:
            self.assertEqual(runner._opts['bootstrap_python'], None)

            if PY2:
                self.assertEqual(runner._bootstrap_python(), [])
                self.assertEqual(runner._bootstrap, [])
            else:
                self.assertEqual(runner._bootstrap_python(),
                                 [['sudo yum install -y python34']])
                self.assertEqual(runner._bootstrap,
                                 [['sudo yum install -y python34']])

    def test_bootstrap_python_switch(self):
        mr_job = MRTwoStepJob(['-r', 'emr', '--bootstrap-python'])

        with no_handlers_for_logger('mrjob.emr'):
            stderr = StringIO()
            log_to_stream('mrjob.emr', stderr)

            with mr_job.make_runner() as runner:
                self.assertEqual(runner._opts['bootstrap_python'], True)

                if PY2:
                    self.assertEqual(runner._bootstrap_python(), [])
                    self.assertEqual(runner._bootstrap, [])
                    self.assertIn('bootstrap_python does nothing in Python 2',
                                  stderr.getvalue())
                else:
                    self.assertEqual(runner._bootstrap_python(),
                                     [['sudo yum install -y python34']])
                    self.assertEqual(runner._bootstrap,
                                     [['sudo yum install -y python34']])
                    self.assertNotIn('bootstrap', stderr.getvalue())

    def test_no_bootstrap_python_switch(self):
        mr_job = MRTwoStepJob(['-r', 'emr', '--no-bootstrap-python'])
        with mr_job.make_runner() as runner:
            self.assertEqual(runner._opts['bootstrap_python'], False)
            self.assertEqual(runner._bootstrap_python(), [])
            self.assertEqual(runner._bootstrap, [])

    def _test_old_ami_version(self, ami_version):
        mr_job = MRTwoStepJob(['-r', 'emr', '--ami-version', ami_version])

        with no_handlers_for_logger('mrjob.emr'):
            stderr = StringIO()
            log_to_stream('mrjob.emr', stderr)

            with mr_job.make_runner() as runner:
                if PY2:
                    self.assertEqual(runner._bootstrap_python(), [])
                    self.assertEqual(runner._bootstrap, [])
                else:
                    self.assertEqual(runner._bootstrap_python(),
                                     [['sudo yum install -y python34']])
                    self.assertEqual(runner._bootstrap,
                                     [['sudo yum install -y python34']])
                    self.assertIn('will probably not work', stderr.getvalue())

    def test_ami_version_3_6_0(self):
        self._test_old_ami_version('3.6.0')

    def test_ami_version_latest(self):
        self._test_old_ami_version('latest')

    def test_bootstrap_python_comes_before_bootstrap(self):
        with patch('mrjob.emr.EMRJobRunner._bootstrap_python',
                   return_value=[['sudo yum install-python-already']]):
            mr_job = MRTwoStepJob(['-r', 'emr', '--bootstrap', 'true'])

            with mr_job.make_runner() as runner:
                self.assertEqual(
                    runner._bootstrap,
                    [['sudo yum install-python-already'], ['true']])


class EMRTagsTestCase(MockBotoTestCase):

    def test_emr_tags_option_dict(self):
        job = MRWordCount([
            '-r', 'emr',
            '--emr-tag', 'tag_one=foo',
            '--emr-tag', 'tag_two=bar'])

        with job.make_runner() as runner:
            self.assertEqual(runner._opts['emr_tags'],
                            {'tag_one': 'foo', 'tag_two': 'bar'})

    def test_command_line_overrides_config(self):
        EMR_TAGS_MRJOB_CONF = {'runners': {'emr': {
            'check_emr_status_every': 0.00,
            's3_sync_wait_time': 0.00,
            'emr_tags': {
                'tag_one': 'foo',
                'tag_two': None,
                'tag_three': 'bar',
            },
        }}}

        job = MRWordCount(['-r', 'emr', '--emr-tag', 'tag_two=qwerty'])

        with mrjob_conf_patcher(EMR_TAGS_MRJOB_CONF):
            with job.make_runner() as runner:
                self.assertEqual(runner._opts['emr_tags'],
                    {'tag_one': 'foo',
                     'tag_two': 'qwerty',
                     'tag_three': 'bar'})

    def test_emr_tags_get_created(self):
        cluster = self.run_and_get_cluster('--emr-tag', 'tag_one=foo',
                                           '--emr-tag', 'tag_two=bar')

        # tags should be in alphabetical order by key
        self.assertEqual(cluster.tags, [
            MockEmrObject(key='tag_one', value='foo'),
            MockEmrObject(key='tag_two', value='bar'),
        ])

    def test_blank_tag_value(self):
        cluster = self.run_and_get_cluster('--emr-tag', 'tag_one=foo',
                                           '--emr-tag', 'tag_two=')

        # tags should be in alphabetical order by key
        self.assertEqual(cluster.tags, [
            MockEmrObject(key='tag_one', value='foo'),
            MockEmrObject(key='tag_two', value=''),
        ])

    def test_tag_values_can_be_none(self):
        runner = EMRJobRunner(conf_paths=[], emr_tags={'tag_one': None})
        cluster_id = runner.make_persistent_cluster()

        mock_cluster = self.mock_emr_clusters[cluster_id]
        self.assertEqual(mock_cluster.tags, [
            MockEmrObject(key='tag_one', value=''),
        ])

    def test_persistent_cluster(self):
        args = ['--emr-tag', 'tag_one=foo',
                '--emr-tag', 'tag_two=bar']

        with self.make_runner(*args) as runner:
            cluster_id = runner.make_persistent_cluster()

        mock_cluster = self.mock_emr_clusters[cluster_id]
        self.assertEqual(mock_cluster.tags, [
            MockEmrObject(key='tag_one', value='foo'),
            MockEmrObject(key='tag_two', value='bar'),
        ])


class IAMEndpointTestCase(MockBotoTestCase):

    def test_default(self):
        runner = EMRJobRunner()

        iam_conn = runner.make_iam_conn()
        self.assertEqual(iam_conn.host, 'iam.amazonaws.com')

    def test_explicit_iam_endpoint(self):
        runner = EMRJobRunner(iam_endpoint='iam.us-gov.amazonaws.com')

        iam_conn = runner.make_iam_conn()
        self.assertEqual(iam_conn.host, 'iam.us-gov.amazonaws.com')

    def test_iam_endpoint_option(self):
        mr_job = MRJob(
            ['-r', 'emr', '--iam-endpoint', 'iam.us-gov.amazonaws.com'])

        with mr_job.make_runner() as runner:
            iam_conn = runner.make_iam_conn()
            self.assertEqual(iam_conn.host, 'iam.us-gov.amazonaws.com')
