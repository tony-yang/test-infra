# Copyright 2017 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generates a SQLite DB containing test data downloaded from GCS."""


import argparse
import logging
import os
import random
import re
import signal
import sys
import time
import urllib.parse
from xml.etree import cElementTree as ET

import google.auth
from google.auth.transport.requests import AuthorizedSession
import multiprocessing
import multiprocessing.pool
import requests
import ruamel.yaml as yaml

import model


def pad_numbers(string):
    """Modify a string to make its numbers suitable for natural sorting."""
    return re.sub(r'\d+', lambda m: m.group(0).rjust(16, '0'), string)

WORKER_CLIENT = None  # used for multiprocessing

class GCSClient:
    def __init__(self, jobs_dir, metadata=None, session=requests.Session()):
        self.jobs_dir = jobs_dir
        self.metadata = metadata or {}
        # credentials, project = google.auth.default()
        # # self.session = requests.Session()
        # self.session = AuthorizedSession(credentials)
        self.session = session

    def _request(self, path, params, as_json=True):
        """GETs a JSON resource from GCS, with retries on failure.

        Retries are based on guidance from
        cloud.google.com/storage/docs/gsutil/addlhelp/RetryHandlingStrategy

        """
        url = 'https://www.googleapis.com/storage/v1/b/%s' % path
        for retry in range(23):
            try:
                print('### make db 59 url = {} and param = {}'.format(url, params))
                # resp = self.session.get(url, params=params, stream=False)
                resp = self.session.get(url, params=params, stream=False)
                print('     resp = {}'.format(resp))
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    return None
                resp.raise_for_status()
                if as_json:
                    print('     resp json = {}'.format(resp.json()))
                    return resp.json()
                print('     resp text = {}'.format(resp.text))
                return resp.text
            except requests.exceptions.RequestException:
                logging.exception('request failed %s', url)
            time.sleep(random.random() * min(60, 2 ** retry))

    @staticmethod
    def _parse_uri(path):
        if not path.startswith('gs://'):
            raise ValueError("Bad GCS path")
        bucket, prefix = path[5:].split('/', 1)
        return bucket, prefix

    def get(self, path, as_json=False):
        """Get an object from GCS."""
        bucket, path = self._parse_uri(path)
        tmp = '%s/o/%s' % (bucket, urllib.parse.quote(path, ''))
        print('#### make db 85 bucket = {} path = {}'.format(bucket, path))
        print('     tmp = {}'.format(tmp))
        # return self._request(tmp, {'alt': 'media'}, as_json=as_json)
        full_path = os.path.join(bucket, 'o', urllib.parse.quote(path, ''))
        return self._request(full_path, {'alt': 'media'}, as_json=as_json)

    def ls(self, path, dirs=True, files=True, delim=True, item_field='name'):
        """Lists objects under a path on gcs."""
        # pylint: disable=invalid-name

        bucket, path = self._parse_uri(path)
        params = {'prefix': path, 'fields': 'nextPageToken'}
        print('##### make db 89 bucket = {} path = {} and params = {}'.format(bucket, path, params))
        if delim:
            params['delimiter'] = '/'
            if dirs:
                params['fields'] += ',prefixes'
        if files:
            params['fields'] += ',items(%s)' % item_field
        while True:
            resp = self._request('%s/o' % bucket, params)
            print('#### make db 100 resp = {}'.format(resp))
            if resp is None:  # nothing under path?
                return
            for prefix in resp.get('prefixes', []):
                yield 'gs://%s/%s' % (bucket, prefix)
            for item in resp.get('items', []):
                if item_field == 'name':
                    yield 'gs://%s/%s' % (bucket, item['name'])
                else:
                    yield item[item_field]
            if 'nextPageToken' not in resp:
                break
            params['pageToken'] = resp['nextPageToken']

    def ls_dirs(self, path):
        return self.ls(path, dirs=True, files=False)

    def _ls_junit_paths(self, build_dir):
        """Lists the paths of JUnit XML files for a build."""
        url = '%sartifacts/' % (build_dir)
        for path in self.ls(url):
            if re.match(r'.*/junit.*\.xml$', path):
                yield path

    def get_junits_from_build(self, build_dir):
        """Generates all tests for a build."""
        files = {}
        assert not build_dir.endswith('/')
        for junit_path in self._ls_junit_paths(build_dir + '/'):
            files[junit_path] = self.get(junit_path)
        return files

    def _get_jobs(self):
        """Generates all jobs in the bucket."""
        print('#### make_db 130 _get_jobs')
        for job_path in self.ls_dirs(self.jobs_dir):
            yield os.path.basename(os.path.dirname(job_path))

    def _get_builds(self, job):
        '''Returns whether builds are precise (guarantees existence)'''
        if self.metadata.get('sequential', True):
            try:
                latest_build = int(self.get('%s%s/latest-build.txt'
                                            % (self.jobs_dir, job)))
            except (ValueError, TypeError):
                pass
            else:
                return False, (str(n) for n in range(latest_build, 0, -1))
        # Invalid latest-build or bucket is using timestamps
        build_paths = self.ls_dirs('%s%s/' % (self.jobs_dir, job))
        return True, sorted(
            (os.path.basename(os.path.dirname(b)) for b in build_paths),
            key=pad_numbers, reverse=True)

    def get_started_finished(self, job, build):
        print('##### make_db 150 job = {} build = {}'.format(job, build))
        print('     self metadata get pr = {}'.format(self.metadata.get('pr')))
        if self.metadata.get('pr'):
            print('    yes pr')
            dir_path = os.path.join(self.jobs_dir, 'directory', job, '{}.txt'.format(build))
            # build_dir = self.get('%s/directory/%s/%s.txt' % (self.jobs_dir, job, build)).strip()
            build_dir = self.get(dir_path).strip()
        else:
            print('    no pr')
            build_dir = os.path.join('{}{}'.format(self.jobs_dir, job), build)
            # build_dir = '%s%s/%s' % (self.jobs_dir, job, build)
        print('#### make_db 155 build dir  ={}'.format(build_dir))
        started = self.get('%s/started.json' % build_dir, as_json=True)
        finished = self.get('%s/finished.json' % build_dir, as_json=True)
        print('#### make db 166 started = {}'.format(started))
        print('#### make db 167 started = {}'.format(finished))
        return build_dir, started, finished

    def get_builds(self, builds_have):
        """Generates all (job, build) pairs ever."""
        print('##### make_db 160 builds_have = {}'.format(builds_have))
        print('     self metadata = {}'.format(self.metadata))
        if self.metadata.get('pr'):
            path = os.path.join(self.jobs_dir, 'directory/')
            files = self.ls(path, delim=False)
            for fname in files:
                if fname.endswith('.txt') and 'latest-build' not in fname:
                    job, build = fname[:-4].split('/')[-2:]
                    if (job, build) in builds_have:
                        continue
                    yield job, build
            return
        for job in self._get_jobs():
            print('### job = {}'.format(job))
            if job in ('pr-e2e-gce', 'maintenance-ci-testgrid-config-upload'):
                continue  # garbage.
            have = 0
            precise, builds = self._get_builds(job)
            for build in builds:
                if (job, build) in builds_have:
                    have += 1
                    if have > 40 and not precise:
                        break
                    continue
                yield job, build


def mp_init_worker(jobs_dir, metadata, client_class, use_signal=True, session=requests.Session()):
    """
    Initialize the environment for multiprocessing-based multithreading.
    """

    if use_signal:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Multiprocessing doesn't allow local variables for each worker, so we need
    # to make a GCSClient global variable.
    global WORKER_CLIENT  # pylint: disable=global-statement
    WORKER_CLIENT = client_class(jobs_dir, metadata)

def get_started_finished(job_info):
    (job, build) = job_info
    try:
        return WORKER_CLIENT.get_started_finished(job, build)
    except:
        logging.exception('failed to get tests for %s/%s', job, build)
        raise

def get_junits(build_info):
    (build_id, gcs_path) = build_info
    try:
        junits = WORKER_CLIENT.get_junits_from_build(gcs_path)
        return build_id, gcs_path, junits
    except:
        logging.exception('failed to get junits for %s', gcs_path)
        raise


def get_builds(db, jobs_dir, metadata, threads, client_class, session=requests.Session()):
    """
    Adds information about tests to a dictionary.

    Args:
        jobs_dir: the GCS path containing jobs.
        metadata: a dict of metadata about the jobs_dir.
        threads: how many threads to use to download build information.
        client_class: a constructor for a GCSClient (or a subclass).
    """
    gcs = client_class(jobs_dir, metadata, session)

    print('Loading builds from %s' % jobs_dir)
    sys.stdout.flush()

    builds_have = db.get_existing_builds(jobs_dir)
    print('already have %d builds' % len(builds_have))
    sys.stdout.flush()

    jobs_and_builds = gcs.get_builds(builds_have)
    print('##### make_db 238 jobs and builds = {}'.format(jobs_and_builds))
    pool = None
    if threads > 1:
        pool = multiprocessing.Pool(threads, mp_init_worker,
                                    (jobs_dir, metadata, client_class, True, session))
        builds_iterator = pool.imap_unordered(
            get_started_finished, jobs_and_builds)
    else:
        global WORKER_CLIENT  # pylint: disable=global-statement
        WORKER_CLIENT = gcs
        builds_iterator = (
            get_started_finished(job_build) for job_build in jobs_and_builds)

    try:
        print('Starting a loop of builds iterator')
        for n, (build_dir, started, finished) in enumerate(builds_iterator):
            print('#### make db 268 build dir = {}'.format(build_dir))
            if started or finished:
                db.insert_build(build_dir, started, finished)
            if n % 200 == 0:
                db.commit()
    except KeyboardInterrupt:
        if pool:
            pool.terminate()
        raise
    else:
        if pool:
            pool.close()
            pool.join()
    db.commit()


def remove_system_out(data):
    """Strip bloated system-out annotations."""
    if 'system-out' in data:
        try:
            root = ET.fromstring(data)
            for parent in root.findall('*//system-out/..'):
                for child in parent.findall('system-out'):
                    parent.remove(child)
            return ET.tostring(root, 'unicode')
        except ET.ParseError:
            pass
    return data


def download_junit(db, threads, client_class, session=requests.Session()):
    """Download junit results for builds without them."""
    print("Downloading JUnit artifacts.")
    sys.stdout.flush()
    builds_to_grab = db.get_builds_missing_junit()
    pool = None
    if threads > 1:
        pool = multiprocessing.pool.ThreadPool(
            threads, mp_init_worker, ('', {}, client_class, False, session))
        test_iterator = pool.imap_unordered(
            get_junits, builds_to_grab)
    else:
        global WORKER_CLIENT  # pylint: disable=global-statement
        WORKER_CLIENT = client_class('', {}, session)
        test_iterator = (
            get_junits(build_path) for build_path in builds_to_grab)
    for n, (build_id, build_path, junits) in enumerate(test_iterator, 1):
        print('%d/%d' % (n, len(builds_to_grab)),
              build_path, len(junits), len(''.join(junits.values())))
        junits = {k: remove_system_out(v) for k, v in junits.items()}

        db.insert_build_junits(build_id, junits)
        if n % 100 == 0:
            db.commit()
    db.commit()
    if pool:
        pool.close()
        pool.join()


def main(db, jobs_dirs, threads, get_junit, client_class=GCSClient, session=requests.Session()):
    """Collect test info in matching jobs."""
    # get_builds(db, 'gs://kubernetes-jenkins/pr-logs', {'pr': True},
                # threads, client_class)
    for bucket, metadata in jobs_dirs.items():
        print("### make_db 318 bucket = {} and metadata = {}".format(bucket, metadata))
        if not bucket.endswith('/'):
            bucket += '/'
        print('#### make db main 348 session = {}'.format(session))
        get_builds(db, bucket, metadata, threads, client_class, session)
    if get_junit:
        download_junit(db, threads, client_class, session)


def get_options(argv):
    """Process command line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--buckets',
        help='YAML file with GCS bucket locations',
        required=True,
    )
    parser.add_argument(
        '--threads',
        help='number of concurrent threads to download results with',
        default=32,
        type=int,
    )
    parser.add_argument(
        '--junit',
        action='store_true',
        help='Download JUnit results from each build'
    )
    return parser.parse_args(argv)


if __name__ == '__main__':
    credentials, _ = google.auth.default()
    auth_session = AuthorizedSession(credentials)
    OPTIONS = get_options(sys.argv[1:])
    main(model.Database(),
         yaml.safe_load(open(OPTIONS.buckets)),
         OPTIONS.threads,
         OPTIONS.junit,
         session=auth_session)
