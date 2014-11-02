from argparse import ArgumentParser
from collections import defaultdict
from os.path import join, basename, relpath
from csv import writer, DictReader
from StringIO import StringIO
from logging import getLogger
from os import environ
from time import time
from glob import glob

from . import paths, jobs, ConformResult, S3

parser = ArgumentParser(description='Run some source files.')

parser.add_argument('bucketname',
                    help='Required S3 bucket name.')

parser.add_argument('-a', '--access-key', default=environ.get('AWS_ACCESS_KEY_ID', None),
                    help='Optional AWS access key name. Defaults to value of AWS_ACCESS_KEY_ID environment variable.')

parser.add_argument('-s', '--secret-key', default=environ.get('AWS_SECRET_ACCESS_KEY', None),
                    help='Optional AWS secret key name. Defaults to value of AWS_SECRET_ACCESS_KEY environment variable.')

parser.add_argument('-l', '--logfile', help='Optional log file name.')

def main():
    args = parser.parse_args()
    
    jobs.setup_logger(args.logfile)
    s3 = S3(args.access_key, args.secret_key, args.bucketname)
    
    run_name = '{:.3f}'.format(time())
    
    return process(s3, paths.sources, run_name)

def read_state(s3, sourcedir):
    '''
    '''
    state_key = s3.get_key('state.txt')
    
    if state_key:
        state_link = state_key.get_contents_as_string()
        state_key = s3.get_key(state_link.strip())
    
    # Use default times of 'zzz' because we're pessimistic about the unknown.
    states = defaultdict(lambda: dict(cache_time='zzz', process_time='zzz'))

    if state_key:
        getLogger('openaddr').debug('Found state in {}'.format(state_key.name))

        state_file = StringIO(state_key.get_contents_as_string())
        rows = DictReader(state_file, dialect='excel-tab')
        
        for row in rows:
            key = join(sourcedir, row['source'])
            states[key] = dict(cache=row['cache'],
                               version=row['version'],
                               fingerprint=row['fingerprint'],
                               cache_time=row['cache time'],
                               process_time=row['process time'])
    
    return states

def process(s3, sourcedir, run_name):
    '''
    '''
    # Find existing cache information
    source_extras1 = read_state(s3, sourcedir)
    getLogger('openaddr').info('Loaded {} sources from state.txt'.format(len(source_extras1)))

    # Cache data, if necessary
    source_files1 = glob(join(sourcedir, '*.json'))
    source_files1.sort(key=lambda s: source_extras1[s]['cache_time'], reverse=True)
    results1 = jobs.run_all_caches(source_files1, source_extras1, s3)
    
    # Proceed only with sources that have a cache
    source_files2 = [s for s in source_files1 if results1[s].cache]
    source_files2.sort(key=lambda s: source_extras1[s]['process_time'], reverse=True)
    source_extras2 = dict([(s, results1[s].todict()) for s in source_files2])
    results2 = jobs.run_all_conforms(source_files2, source_extras2, s3)

    # Gather all results
    write_state(s3, sourcedir, run_name, source_files1, results1, results2)

def write_state(s3, sourcedir, run_name, source_files1, results1, results2):
    '''
    '''
    state_file = StringIO()
    out = writer(state_file, dialect='excel-tab')
    
    out.writerow(('source', 'cache', 'version', 'fingerprint', 'cache time', 'processed', 'process time'))
    
    for source in source_files1:
        result1 = results1[source]
        result2 = results2.get(source, ConformResult.empty())
    
        out.writerow((relpath(source, sourcedir), result1.cache,
                      result1.version, result1.fingerprint, result1.elapsed,
                      result2.processed, result2.elapsed))
    
    state_data = state_file.getvalue()
    state_link = 'runs/{}/state.txt'.format(run_name)
    state_args = dict(policy='public-read', headers={'Content-Type': 'text/plain'})

    s3.new_key(state_link).set_contents_from_string(state_data, **state_args)
    s3.new_key('state.txt').set_contents_from_string(state_link, **state_args)
    
    getLogger('openaddr').info('Wrote {} sources to state.txt'.format(len(source_files1)))

if __name__ == '__main__':
    exit(main())
