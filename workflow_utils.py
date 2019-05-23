#!/usr/bin/env python

# * Preamble
# ** module comment

"""
Utilities for dealing with workflows, including cloud workflows.

Commands in this module help with the following:
   - jointly version code and data using git-annex ( https://git-annex.branchable.com/ ) and DataLad ( https://www.datalad.org/ )
   - keep track of data located in various places (dnanexus, AWS, GCS) in a unified and uniform way
   - keep track of analyses that have been run, storing each analysis' details in a single self-contained 'analysis directory'
     within a git/git-annex repository.
   - re-running an analysis, or a group of analyses, on any cloud (regardless of where they were originally run),
     while applying specified modifications to the parameters.  In particular, this enables curating sets of benchmark
     analyses, represented as a git subdir containing a group of analyses, and re-running them with a given parameter
     modification.

In combination, these utilities enable much of what DNAnexus enables, but:
   - without forcing all analyses and data to reside in one vendor
   - providing a uniform interface that works the same across vendors and clouds, as well as analyses run on
     UGER or Odyssey or locally
   - allowing the use of spot/preemptible instances (which DNAnexus disallows)
   - allowing easy archiving/unarchiving of data, using git-annex commands
   - using all of git's normal mechanisms, such as branches and submodules, to organize things

Terms and abbreviations:
   workflow:
       currently this means a WDL workflow, normally from pipes/WDL/workflows in a specific
       version of vira-ngs.
   analysis:
       particular execution of a particular workflow.  same concept as a DNAnexus analysis
       (https://wiki.dnanexus.com/API-Specification-v1.0.0/Workflows-and-Analyses).
       Note that Cromwell documentation uses 'workflow' for both workflow definitions and their
       instantiations; we'll keep to DNAnexus' usage for better clarity.
   specified inputs: 
       inputs explicitly provided to a given analysis.  together with the workflow's default inputs, the form `full inputs`.
       corresponds to DNAnexus notion of 'runInputs'
   full inputs:
       full inputs to an analysis, comprised of `specified inputs` supplemented where needed by workflow's defaults.
       corresponds to DNAnexus notion of 'originalInputs'.

   lcpath:
       local or cloud path, representing as a string

   fmdata:
       file metadata (size, md5, etc) associated with a file, represented as a dict;
       different from analysis metadata, which is associated with an analysis
       and includes things like analysis labels.

   jpath:
       path navigating through a parsed json structure: tuple of keys and indices to get from the root to a leaf value.

   crogit:
       a job queueing system implemented here, based on Cromwell and git/git-annex.

TODO:
  - do all bulk ops on temp clones of the repo, in a temp branch, then merge if succeed.
  - use fewer dirs for files?  one dir per file leads to inode explosion.
  - make things work with v7 git-annex repos
"""

__author__ = "ilya@broadinstitute.org"
__commands__ = []

# ** imports

# *** built-ins

import argparse
import logging
import json
import yaml
import subprocess
import os
import os.path
import shutil
import glob
import collections
import time
import getpass
import uuid
import functools
import itertools
import operator
import builtins
import datetime
import hashlib
import pipes
import shlex
import sys
import re
import random
import urllib
try:
    from urllib import urlencode, pathname2url
except ImportError:
    from urllib.parse import urlencode
    from urllib.request import pathname2url
try:
    import SimpleHTTPServer
    import SocketServer
except ImportError:
    import http.server as SimpleHTTPServer
    SocketServer = SimpleHTTPServer.socketserver

import traceback
import copy
from pprint import pformat
import binascii
import concurrent.futures

# *** 3rd-party

import jmespath
import jmespath.functions

import dxpy
import dxpy.bindings.dxfile_functions

import uritools
import pytz
import contextlib2

import yattag
import pandas as pd
import matplotlib
matplotlib.use('svg')
import matplotlib.pyplot as pp

# *** intra-module
import util.cmd
import util.file
import util.misc
import util.version
import tools.git_annex
import tools.gcloud
import tools.docker
import tools.cromwell
import tools.muscle

_log = logging.getLogger(__name__)

#logging.basicConfig(format="%(asctime)s - %(module)s:%(lineno)d:%(funcName)s - %(levelname)s - %(message)s")
_log.setLevel(logging.INFO)

# * Utils
# ** Generic utils

# *** type identification

_str_type = basestring if hasattr(builtins, 'basestring') else str
_long_type = long if hasattr(builtins, 'long') else int
_scalar_types = (type(None), _str_type, _long_type, int, bool, float)
if hasattr(builtins, 'unicode'):
    _scalar_types += (unicode,)

def _is_str(obj):
    """Test if obj is a string type, in a python2/3 compatible way.
    From https://stackoverflow.com/questions/4232111/stringtype-and-nonetype-in-python3-x
    """
    return isinstance(obj, _str_type)

def _is_mapping(obj):
    return isinstance(obj, collections.Mapping)

def _is_scalar(val):
    isinstance(val, _scalar_types)

# *** maps and dicts utils

def _maps(obj, *keys):
    return _is_mapping(obj) and all(k in obj for k in keys)

def _dict_subset(d, keys):
    """Return a newly allocated shallow copy of a mapping `d` restricted to keys in `keys`."""
    return {k: v for k, v in d.items() if k in keys}

def _ord_dict(*args):
    """Constructs a collections.OrderedDict from `args`.  Using ordered dicts gives better
    reproducibility."""
    return collections.OrderedDict(args)

def _ord_dict_merge(dicts):
    dicts_items = [list(d.items()) for d in dicts]
    return _ord_dict(*functools.reduce(operator.concat, dicts_items, []))

def _dict_rename_key(d, old_key, new_key):
    _log.debug('RENAMING %s (%s) TO %s (%s)', old_key, type(old_key), new_key, type(new_key))
    assert old_key in d, '{} (type {}) not in {}'.format(old_key, type(old_key), d)
    assert new_key not in d, '{} (type {}) already in {}'.format(new_key, type(new_key), d)
    if new_key != old_key:
        d[new_key] = d[old_key]
        del d[old_key]
    return d

# *** json processing

# **** jmespath-related

# 1. Create a subclass of functions.Functions.
#    The function.Functions base class has logic
#    that introspects all of its methods and automatically
#    registers your custom functions in its function table.
class JmesPathCustomFunctions(jmespath.functions.Functions):

    """Custom functions for use in jmespath queries"""

    @jmespath.functions.signature({'types': ['string']}, {'types': ['string']}, {'types': ['string']})
    def _func_re_sub(self, pattern, repl, s):
        """Substitute a regexp in a string"""
        return re.sub(pattern, repl, s)

    @jmespath.functions.signature({'types': ['expref']}, {'types': ['object']})
    def _func_map_keys(self, expref, obj):
        result = collections.OrderedDict()
        for key, val in obj.items():
            key_mapped = expref.visit(expref.expression, key)
            result[key_mapped] = val
        return result

    @jmespath.functions.signature({'types': ['object']}, {'types': ['string']}, {'types': ['string']})
    def _func_replace_keys(self, obj, patt, repl):
        """Replace a string in all keys of an object"""
        return { k.replace(patt, repl): v for k, v in obj.items()}

# end: class JmesPathCustomFunctions(jmespath.functions.Functions)

def _qry_json(json_data, jmespath_expr, dflt=None):
    """Return the result of a jmespath query `jmespath_expr` on `json_data`,
    or `dflt` if the query returns None."""
    res =  jmespath.search(jmespath_expr, json_data,
                           jmespath.Options(dict_cls=collections.OrderedDict,
                                            custom_functions=JmesPathCustomFunctions()))
    return res if res is not None else dflt

# **** json I/O

def _pretty_print_json(json_dict, sort_keys=True):
    """Return a pretty-printed version of a dict converted to json, as a string."""
    return json.dumps(json_dict, indent=4, separators=(',', ': '), sort_keys=sort_keys)

def _write_json(fname, **json_dict):
    util.file.dump_file(fname=fname, value=_pretty_print_json(json_dict))

def _load_dict_sorted(d):
    return collections.OrderedDict(sorted(d.items()))

def _json_loads(s):
    return json.loads(s.strip(), object_hook=_load_dict_sorted, object_pairs_hook=collections.OrderedDict)

def _json_loadf(fname):
    return _json_loads(util.file.slurp_file(fname, maxSizeMb=1000))

# *** timestamps processing

_tz_eastern = pytz.timezone('US/Eastern')
def _isoformat_datetime(dt):
    return _tz_eastern.localize(dt).isoformat('T')

def _isoformat_ago(**timedelta_args):
    return (datetime.datetime.now(_tz_eastern) - datetime.timedelta(**timedelta_args)).isoformat('T')

# *** running commands

def _noquote(s):
    return '_noquote:' + str(s)

def _quote(s):
    s = str(s)
    if s.startswith('_noquote:'): return s[len('_noquote:'):]
    return pipes.quote(s) if hasattr(pipes, 'quote') else shlex.quote(s)

def _make_cmd(cmd, *args):
    _log.debug('ARGS=%s', args)
    return ' '.join([cmd] + [_quote(str(arg)) for arg in args if arg not in (None, '')])

def _run(cmd, *args, **kw):
    cmd = _make_cmd(cmd, *args)
    _log.info('running command: %s cwd=%s kw=%s', cmd, os.getcwd(), kw)
    beg_time = time.time()
    succeeded = False
    try:
        subprocess.check_call(cmd, shell=True, **kw)
        succeeded = True
    finally:
        _log.info('command (cwd={}, kw={}) {} in {}s: {}'.format(os.getcwd(), kw, 'SUCCEEDED' if succeeded else 'FAILED',
                                                                 time.time()-beg_time, cmd))

def _run_succeeds(cmd, *args, **kw):
    try:
        _run(cmd, *args, **kw)
        return True
    except subprocess.CalledProcessError:
        return False

def _run_get_output(cmd, *args, **kw):
    cmd = _make_cmd(cmd, *args)
    _log.info('running command: %s cwd=%s kw=%s', cmd, os.getcwd(), kw)
    beg_time = time.time()
    succeeded = False
    try:
        output = subprocess.check_output(cmd, shell=True, **kw)
        succeeded = True
        return output.strip()
    finally:
        _log.info('command (cwd={}, kw={}) {} in {}s: {}'.format(os.getcwd(), kw, 'SUCCEEDED' if succeeded else 'FAILED',
                                                                 time.time()-beg_time, cmd))

def _run_get_json(cmd, *args, **kw):
    return _json_loads(_run_get_output(cmd, *args, **kw))

# *** misc utils

def _is_under_dir(path, base_dir):
    """Tests whether `path` is somewhere in the dir tree under `base_dir`"""
    path_parts = os.path.realpath(path).split(os.path.sep)
    base_parts = os.path.realpath(base_dir).split(os.path.sep)
    return path_parts[:len(base_parts)] == base_parts

def _md5_base64(s):
    return hashlib.md5(s).digest().encode('base64').strip()

def _is_valid_md5(s):
    return _is_str(s) and len(s) == 32 and set(s) <= set('0123456789ABCDEF')

@util.misc.memoize
def _running_on_aws():
    return 'VIRAL_NGS_ON_AWS' in os.environ or \
        run_succeeds('wget', 'http://169.254.169.254/latest/dynamic/instance-identity/document')

def workflow_utils_init():
    """Install the dependencies: cromwell and dxpy and git-annex."""
    _run('conda install cromwell dxpy git-annex')


def _format_exc(e):
    return ''.join(traceback.format_exception(type(e),
                                              e, e.__traceback__)) \
                                              if hasattr(e, '__traceback__') else ''


# ** AnalysisDir

class AnalysisDir(object):

    """A directory representing an analysis.   Typically a directory in a git-annex repository,
    with analysis metadata in standard format, and analysis files as git-annex files under the 
    analysis directory.

    An analysis directory can represent an analysis in various states: being prepared, submitted,
    finished; succeeded or failed.

    """
    
    METADATA_VERSION = "1.0"

    def __init__(self, path):
        self.path = path

    def is_analysis_dir(self):
        return os.path.isfile(os.path.join(self.path, 'metadata.json'))

# ** DNAnexus-related utils

@util.misc.memoize_persist(to_picklable=functools.partial(json.dumps, separators=(',',':')),
                           from_picklable=_json_loads)
def _dx_describe(dxid):
    return _run_get_json('dx', 'describe', '--verbose', '--details', '--json', dxid)

DX_URI_PFX='dx://'

def _url_to_dxid(url):
    return os.path.splitext(url[len(DX_URI_PFX):].split('/')[0])[0]

def _dx_make_download_url(dxid, duration='2h'):
    return _run_get_output('dx', 'make_download_url', dxid, '--duration', duration)

def _standardize_dx_url(url):
    dxid = _url_to_dxid(url)
    dx_descr = _dx_describe(_url_to_dxid(url))
    return DX_URI_PFX + dxid + '/' + pathname2url(dx_descr['name'])


# ** GCS-related utils

# ** git and git-annex-related utils

def _is_git_link(val):
    """Tests whether `val` points to a file in git"""
    return _maps(val, '$git_link')

def _make_git_links_absolute(d, base_dir):
    """Change any git links within json struct `d` that are relative paths, to be absolute paths, intepreting relative paths
    as relative to `base_dir`."""
    assert os.path.isabs(base_dir)
    def _make_git_link_absolute(val):
        if not _maps(val, '$git_link'):
            return val
        util.misc.chk(not os.path.isabs(val['$git_link']))
        return dict(val, **{'$git_link': os.path.join(base_dir, val['$git_link'])})
    return util.misc.transform_json_data(d, _make_git_link_absolute)

def _make_git_links_relative(d, base_dir):
    """Change any git links within json struct `d` that are absolute paths, to be relative paths, relative to `base_dir`."""
    def _make_git_link_relative(val):
        if not _maps(val, '$git_link'):
            return val
        util.misc.chk(os.path.isabs(val['$git_link']))
        return dict(val, **{'$git_link': os.path.relpath(val['$git_link'], base_dir)})
    return util.misc.transform_json_data(d, _make_git_link_relative)

def _git_annex_get_link_into_annex(f):
    """Follow symlinks as needed, to find the final symlink pointing into the annex"""
    link_target = None
    while os.path.islink(f):
        link_target = os.readlink(f)
        if '.git/annex/objects/' in link_target:
            break
        if os.path.isabs(link_target):
            f = link_target
        else:
            f = os.path.join(os.path.dirname(f), link_target)
    return f, link_target

def _git_annex_lookupkey(f):
    f, link_target = _git_annex_get_link_into_annex(f)
    if link_target:
        link_target_basename = os.path.basename(link_target)
        if link_target_basename.startswith('MD5-') or link_target_basename.startswith('MD5E-'):
            return link_target_basename
            #md5_start = link_target_basename.index('--')+2
            #return link_target_basename[md5_start:md5_start+32]

    return _run_get_output('git', 'annex', 'lookupkey', f, cwd=os.path.dirname(os.path.abspath(f)))

def _git_link_md5(git_link):
    """Return the md5 of the file pointed to by git_link"""
    # use readlink here for speed
    assert _is_git_link(git_link)
    if 'md5' not in git_link:
        ga_key = _git_annex_lookupkey(git_link['$git_link'])
        assert ga_key.startswith('MD5E-')
        md5_start = ga_key.index('--')+2
        git_link['md5'] = ga_key[md5_start:md5_start+32]
    return git_link['md5']

def _git_annex_get(f):
    """Ensure the file exists in the local annex.  Unlike git-annex-get, follows symlinks and 
    will get the file regardless of what the current dir is."""

    # TODO: what if f is a dir, or list of dirs?  including symlinks?
    # then, to preserve git-annex-get semantics, need to 

    assert os.path.islink(f)
    f, link_target = _git_annex_get_link_into_annex(f)
    if not os.path.isfile(f):
        _run('git', 'annex', 'get', os.path.basename(f), cwd=os.path.dirname(os.path.abspath(f)))
    assert os.path.isfile(f)

def _git_annex_add(f):
    _run('git', 'annex', 'add', f)

def _git_annex_get_metadata(key, field):
    ga_mdata = _run_get_json('git', 'annex', 'metadata', '--json', '--key='+key)
    return ga_mdata.get('fields', {}).get(field, [])

def _git_annex_set_metadata(key, field, val):
    _run('git', 'annex', 'metadata', '--key='+key, '-s', field + '=' + val)

def _git_annex_checkpresentkey(key, remote, git_file_path):
    return _run_succeeds('git annex checkpresentkey', key, remote, cwd=os.path.dirname(os.path.abspath(git_file_path))) if remote else _run_succeeds('git annex checkpresentkey', key,
                                                                                                cwd=os.path.dirname(os.path.abspath(git_file_path)))

def _git_annex_get_url_key(url):
    """Return the git-annex key for a url"""
    _log.debug('get-url-key %s', url)
    assert uritools.isuri(url)
    url = _minimize_url(url)
    assert uritools.isuri(url)

    url_parts = uritools.urisplit(url)
    _log.info('url is %s url_parts are %s', url, url_parts)
    path_type = type(url_parts.path)
    url_parts = url_parts._replace(path=path_type(uritools.uriencode(url_parts.path, safe='/')))
    _log.info('now url is %s url_parts are %s', url, url_parts)
    url = uritools.uriunsplit(url_parts)

    assert uritools.isuri(url)
    return 'URL--' + url

    # old way, maybe less abstraction-breaking but slower -- let git-annex compute the key:
    # with util.file.tmp_dir(dir=os.getcwd()) as tmp_d:
    #     f = os.path.join(tmp_d, 'f')
    #     _run('git', 'annex', 'fromkey', '--force', url, f)
    #     key = _git_annex_lookupkey(f)
    #     shutil.rmtree(tmp_d, ignore_errors=True)
    #     return key


# * import_dx_analysis: import DNAnexus analyses as analysis dirs

# ** import_dx_analysis utils
def _resolve_dx_link_to_dx_file_id_or_value(val, dx_analysis_id):
    """Resolve DNAnexus links, including indirect ones that point to the output of an analysis stage,
    to either direct link to a DNAnexus file or a simple value.
    If `val` represents a DNAnexus link to a file, return {$dnanexus_link: file-xxxx}.
    If `val` represents a DNAnexus link to a value, return that value.
    Else, return `val` unchanged.
    """

    #print('parsing val: ', val)
    recurse = functools.partial(_resolve_dx_link_to_dx_file_id_or_value,
                                dx_analysis_id=dx_analysis_id)
    if not _maps(val, '$dnanexus_link'):
        return val

    link = val['$dnanexus_link']
    if _maps(link, 'stage') and ('field' in link or 'outputField' in link):
        _log.debug('link is %s', link)
        linked_analysis_descr = _dx_describe(link.get('analysis', dx_analysis_id))
        linked_field = link['field'] if 'field' in link else link['outputField']
        if link['stage']+'.'+linked_field not in linked_analysis_descr['output']:
            # this can happen if the stage has failed.
            return {'$dnanexus_failed_stage_output': None}
        return recurse(val=linked_analysis_descr['output'][link['stage']+'.'+linked_field])
    elif (_is_str(link) and link.startswith('file-')) or \
         (isinstance(link, collections.Mapping) and 'id' in link \
          and _is_str(link['id']) and link['id'].startswith('file-')):
        if _is_str(link) and link.startswith('file-'):
            dxid = link
        else:
            dxid = link['id']
        return {'$dnanexus_link': dxid}
    raise RuntimeError('Unknown $dnanexus_link: {}'.format(val))

def _resolve_link_using_gathered_filestat(val, git_file_dir, git_annex_tool):
    if not _is_str(val): return val
    git_annex_key = git_annex_tool.get_key_for_url(val, error_if_missing=False)
    if not git_annex_key: return val
    util.file.mkdir_p(git_file_dir)
    fname = os.path.join(git_file_dir, os.path.basename(val))
    git_annex_tool.fromkey(git_annex_key, fname, now=False)
    return {'$git_link': fname, 'git_annex_key': git_annex_key, 'orig_path': val}

def _resolve_link(val, git_file_dir, methods):
    for method in methods:
        result = method(val=val, git_file_dir=git_file_dir)
        if result is not val: return result
    return val

def _resolve_links_in_json_data(val, rel_to_dir, methods, relpath='files'):
    """Given a parsed json structure, replace in it references to files (in various forms) with one uniform
    representation, a 'git link'.  A git link contains a relative path (relative to `analysis_dir`)
    pointing to a git file, typically a git-annex file.
    """
    def handle_node(val, path):
        path = list(functools.reduce(operator.concat, [str(p).split('.') for p in path], []))
        maybe_resolve_link = _resolve_link(val=val,
                                           git_file_dir=os.path.join(rel_to_dir, relpath,
                                                                     *path), methods=methods)
        if _maps(maybe_resolve_link, '$git_link'):
            maybe_resolve_link['$git_link'] = os.path.relpath(maybe_resolve_link['$git_link'], rel_to_dir)
        return maybe_resolve_link
        
    return util.misc.transform_json_data(val, node_handler=handle_node)

def _determine_dx_analysis_docker_img(dnanexus_tool, mdata):
    """Determine the viral-ngs docker image used for a given dx analysis"""
    # determine the viral-ngs version
    DX_CI_PROJECT = 'project-F8PQ6380xf5bK0Qk0YPjB17P'
    executable_descr = dnanexus_tool.describe({'$dnanexus_link': mdata['executable'], 'project': DX_CI_PROJECT})
    util.misc.chk('dxWDL' in executable_descr['tags'])
    if not executable_descr['folder'].startswith('/build/quay.io/broadinstitute/viral-ngs'):
        # the workflow may have been copied from the "Broad viral-ngs dxWDL CI - Public" project.
        # Look at the applets.
        executable_descr = dnanexus_tool.describe(mdata['stages'][0]['execution']['executable'])
    exe_folder_parts = executable_descr['folder'].split('/')
    docker_img = '/'.join(exe_folder_parts[2:5]) + ':' + exe_folder_parts[5]
    docker_tool = tools.docker.DockerTool()
    mdata['labels']['docker_img'] = docker_img
    mdata['labels']['docker_img_hash'] = docker_tool.add_image_hash(docker_img)

def _get_wdl_for_docker_img(docker_img_hash, workflow_name, out_):
    """For given viral-ngs docker image, extract the WDL files corresponding to that image,
    patch the WDL tasks to use that image, prepare the WDL file for the main workflow and a zip file for the
    dependencies, and determine the input spec for the workflow.
    """
    wdl_version_dir = os.path.join('workflow_versions', util.file.string_to_file_name(docker_img_hash), workflow_name)
    if not os.path.isdir(wdl_version_dir):
        util.file.mkdir_p(wdl_version_dir)

        docker_tool = tools.docker.DockerTool()
        _extract_wdl_from_docker_img(docker_img_hash, wdl_version_dir)

        _run('sed -i -- "s|{}|{}|g" *.wdl'.format('quay.io/broadinstitute/viral-ngs',
                                                  docker_tool.strip_image_hash(docker_img_hash)))
        _run('zip imports.zip *.wdl')
        workflow_inputs_spec = _get_workflow_inputs_spec(workflow_name, docker_img=docker_img_hash)

# ** import_dx_analysis impl

def _import_dx_analysis(dx_analysis_id, analysis_dir_pfx, git_annex_tool, dnanexus_tool):
    """Import a DNAnexus analysis into git, in our analysis dir format."""

    analysis_dir = analysis_dir_pfx + dx_analysis_id
    util.file.mkdir_p(analysis_dir)
    mdata_orig = dnanexus_tool.describe(dx_analysis_id)

    # TODO: figure out proper handling for internal runinputs

    # TODO: if workflow is not from the public CI project, see if individual applets are
    # TODO: get the right original WDL, pull in the spec, and see which inputs are part of the
    #    WDL spec.  maybe also, recompile with -inputs ; though, what if dxWDL version changed?  reuse that version.
    #    then have the correspondence of womtool inputs to actual dx inputs.

    # Note also, that can then use that to re-run things on dx, not just locally.

    # TODO: cache the list of remotes (in tools/git_annex)

    _log.info('CALLING RESOLVE_DX_LINKS_IN_DX_ANALYSIS_DESCR')
    mdata = dnanexus_tool.resolve_dx_links_in_dx_analysis_descr(mdata_orig)
    _log.info('RETURNED FROM RESOLVE_DX_LINKS_IN_DX_ANALYSIS_DESCR')
    #    methods = [functools.partial(_resolve_link_dx, dx_analysis_id=dx_analysis_id, _cache={})]
    #    mdata = _resolve_links_in_json_data(val=mdata, rel_to_dir=analysis_dir, methods=methods)

    # 
    # Convert DNAnexus analysis metadata to Cromwell's metadata format,
    # so that all our analysis dirs have analysis metadata in the same format:
    # https://cromwell.readthedocs.io/en/develop/api/RESTAPI/#workflowmetadataresponse
    # Currently only convert a few basic fields.
    # 

    mdata['_metadata_version'] = AnalysisDir.METADATA_VERSION
    mdata['labels'] = _ord_dict(('platform', 'dnanexus'),
                                ('analysis_dir', analysis_dir),
                                ('analysis_id', dx_analysis_id))
    #mdata['workflowName'] = mdata['executableName']

    docker_img = dnanexus_tool.determine_viral_ngs_dx_analysis_docker_img(mdata)
    docker_tool = tools.docker.DockerTool()
    mdata['labels']['docker_img'] = docker_img
    mdata['labels']['docker_img_hash'] = docker_tool.add_image_hash(docker_img)

    _log.info('labels=%s', mdata['labels'])

    stage2name = {}
    for stage in mdata['stages']:
        if _maps(stage, 'id', 'execution'):
            stage2name[stage['id']] = stage['execution']['name']
            if _maps(stage['execution'], 'runInput'):
                for k, v in stage['execution']['runInput'].items():
                    mdata['runInput'][stage['id']+'.'+k] = v
                    _log.info('ADDING RUNINPUT FROM STAGE: %s = %s', stage['id']+'.'+k, v)

    for mdata_rec in ('input', 'output', 'runInput'):
        for k in tuple(mdata[mdata_rec]):
            stage_id = k.split('.')[0]
            if stage_id in stage2name:
                _log.info('RENAMING %s to %s', k, mdata['workflowName']+'.'+stage2name[stage_id]+k[len(stage_id):])
                # TODO: fix dependence on dxWDL internals, specifically that common stage is first
                util.misc.chk(stage2name[stage_id] != 'common' or stage_id == 'stage-0')
                _dict_rename_key(mdata[mdata_rec], k,
                                 mdata['workflowName'] + \
                                 (('.'+stage2name[stage_id]) if stage2name[stage_id] != 'common' else '') + \
                                 k[len(stage_id):])

    for k in tuple(mdata['runInput']):
        if k.startswith(mdata['workflowName']+'.outputs.'):
            _log.info('DROPPING RUNINPUT: %s', k)
            del mdata['runInput'][k]

    mdata['submittedFiles'] = collections.OrderedDict()

    #util.misc.chk_eq(_json_loads(mdata['submittedFiles']['inputs']), mdata['runInput'])

    _dict_rename_key(mdata, 'runInput', 'runInputs')
    _dict_rename_key(mdata, 'input', 'inputs')
    _dict_rename_key(mdata, 'output', 'outputs')
    _dict_rename_key(mdata, 'state', 'status')
    del mdata['originalInput']  # same as 'input'

    if mdata['status'] == 'done':
        mdata['status'] = 'Succeeded'
    elif mdata['status'] == 'failed':
        mdata['status'] = 'Failed'

    submission_datetime = datetime.datetime.fromtimestamp(float(mdata['created']) / 1000.0)
    mdata['submission'] = _isoformat_datetime(submission_datetime)
    

    leaf_jpaths = util.misc.json_gather_leaf_jpaths(mdata)
    str_leaves = list(filter(_is_str, util.misc.map_vals(leaf_jpaths)))
    git_annex_tool.import_urls(str_leaves, ignore_non_urls=True, now=False)
    mdata_rel = _resolve_links_in_json_data(val=mdata, rel_to_dir=analysis_dir,
                                            methods=[functools.partial(_resolve_link_using_gathered_filestat,
                                                                       git_annex_tool=git_annex_tool),
                                            ], relpath='files')

    return (analysis_dir, mdata, mdata_rel)
#    _write_json(mdata_rel_fname, **mdata_rel)
#    _write_json(os.path.join(analysis_dir, 'metadata.json'), **mdata)

def _import_dx_analyses_orig(dx_analysis_ids, analysis_dir_pfx):
    """Import one or more DNAnexus analyses into git, in our analysis dir format."""
    git_annex_tool = tools.git_annex.GitAnnexTool()
    dnanexus_tool = tools.dnanexus.DxTool()
    out = []
    with git_annex_tool.batching() as git_annex_tool:
        for dx_analysis_id in dx_analysis_ids:
            try:
                out.append(_import_dx_analysis(dx_analysis_id, analysis_dir_pfx, git_annex_tool, dnanexus_tool))
            except Exception as e:
                _log.warning('Could not import %s: %s %s', dx_analysis_id, e,
                             ''.join(traceback.format_exception(type(e),
                                                                e, e.__traceback__))
                             if hasattr(e, '__traceback__') else '')
                    
    for analysis_dir, mdata, mdata_rel in out:
        mdata = util.misc.transform_json_data(mdata, functools.partial(util.misc.maybe_wait_for_result, timeout=300))
        mdata_rel = util.misc.transform_json_data(mdata_rel, functools.partial(util.misc.maybe_wait_for_result, timeout=300))
        util.file.mkdir_p(analysis_dir)
        _write_json(os.path.join(analysis_dir, 'metadata_orig.json'), **mdata)
        _write_json(os.path.join(analysis_dir, 'metadata_with_gitlinks.json'), **mdata_rel)
        run_inputs = copy.copy(mdata_rel['runInputs'])
        run_inputs['_docker_img'] = mdata_rel['labels']['docker_img_hash']
        run_inputs['_workflow_name'] = mdata_rel['workflowName']
        _prepare_analysis_crogit_do(inputs=run_inputs,
                                    analysis_dir=mdata_rel['labels']['analysis_dir'],
                                    analysis_labels=mdata_rel['labels'], git_annex_tool=git_annex_tool)


def import_dx_analyses(dx_analysis_ids, analysis_dir_pfx):
    """Import one or more DNAnexus analyses into git, in our analysis dir format."""
    git_annex_tool = tools.git_annex.GitAnnexTool()
    succ = False
    with git_annex_tool._in_tmp_worktree() as branch:
        try:
            _import_dx_analyses_orig(dx_analysis_ids, analysis_dir_pfx)
            git_annex_tool.add_cwd()
            git_annex_tool.execute_git(['commit', '.', '-m', 'temp_commit'])
            _log.info('COMMITTED IMPORT %s', dx_analysis_ids)
            succ = True
        except Exception:
            _log.warning('Failed to import %s', dx_analysis_ids)

def parser_import_dx_analyses(parser=argparse.ArgumentParser(fromfile_prefix_chars='@')):
    parser.add_argument('dx_analysis_ids', metavar='DX_ANALYSIS_ID', nargs='+', help='dnanexus analysis id(s)')
    parser.add_argument('--analysisDirPfx', dest='analysis_dir_pfx', default='pipelines/dxan-',
                        help='analysis dir prefix; analysis id will be added to it.')
    util.cmd.attach_main(parser, import_dx_analyses, split_args=True)
    return parser

__commands__.append(('import_dx_analyses', parser_import_dx_analyses))


def import_one_dx_analysis(dx_analysis_id, analysis_dir_pfx):
    """Import one or more DNAnexus analyses into git, in our analysis dir format."""
    _import_dx_analyses_orig([dx_analysis_id], analysis_dir_pfx)

def parser_import_one_dx_analysis(parser=argparse.ArgumentParser(fromfile_prefix_chars='@')):
    parser.add_argument('dx_analysis_id', metavar='DX_ANALYSIS_ID', help='dnanexus analysis id')
    parser.add_argument('--analysisDirPfx', dest='analysis_dir_pfx', default='pipelines/dxan-',
                        help='analysis dir prefix; analysis id will be added to it.')
    util.cmd.common_args(parser, (('loglevel', None), ('version', None), ('tmp_dir', None)))
    util.cmd.attach_main(parser, import_one_dx_analysis, split_args=True)
    return parser

__commands__.append(('import_one_dx_analysis', parser_import_one_dx_analysis))

def _import_dx_analysis_and_commit(dx_analysis_id, analysis_dir_pfx, script, temp_worktree_dir):
    #util.file.mkdir_p(os.path.join(temp_worktree_dir, 'tmp'))
    log_fname = os.path.join(temp_worktree_dir, analysis_dir_pfx + dx_analysis_id,
                             'import_dx.log')
    util.file.mkdir_p(os.path.dirname(log_fname))
    try:
        with open(log_fname + '.out.txt', 'wt') as _out_stdout, open(log_fname + '.err.txt', 'wt') as _out_stderr:
            _run(script, 'import_one_dx_analysis', '--analysisDirPfx', analysis_dir_pfx,
                 dx_analysis_id, cwd=temp_worktree_dir, stdout=_out_stdout, stderr=_out_stderr)
    finally:
        _run('git', 'annex', 'add', '.', cwd=temp_worktree_dir)
        _run('git', 'commit', '-m', 'imported dx analysis {}'.format(dx_analysis_id), cwd=temp_worktree_dir)

def _set_up_worktree(dx_analysis_id, exit_stack, git_annex_tool, worktree_group_id):
    return (dx_analysis_id,) + exit_stack.enter_context(git_annex_tool._in_tmp_worktree(worktree_group_id=worktree_group_id,
                                                                                        chdir=False))

def import_mult_dx_analyses(dx_analysis_ids, analysis_dir_pfx, set_tmp_dir):
    """Import one or more DNAnexus analyses into git, in our analysis dir format."""

    git_annex_tool = tools.git_annex.GitAnnexTool()
    with contextlib2.ExitStack() as exit_stack:
        executor = exit_stack.enter_context(concurrent.futures.ThreadPoolExecutor(max_workers=min(len(dx_analysis_ids),
                                                                                                  util.misc.available_cpu_count())))
        worktree_group_id = 'impdx_{:06d}'.format(random.randint(0,999999))
        temps = list(executor.map(functools.partial(_set_up_worktree, exit_stack=exit_stack, git_annex_tool=git_annex_tool,
                                                    worktree_group_id=worktree_group_id+'/imp'), dx_analysis_ids))
        script = os.path.join(util.version.get_project_path(), os.path.basename(__file__))
        util.misc.chk(os.path.isabs(script) and os.path.samefile(script, __file__))
        futures = [executor.submit(_import_dx_analysis_and_commit,
                                   dx_analysis_id=dx_analysis_id, analysis_dir_pfx=analysis_dir_pfx,
                                   script=script, temp_worktree_dir=temp_worktree_dir)
                   for dx_analysis_id, temp_branch, temp_worktree_dir in temps]
        wait_res = concurrent.futures.wait(futures)
        done_fail_branches = {'done': [], 'fail': []}
        for future, (dx_analysis_id, temp_branch, temp_worktree_dir) in zip(futures, temps):
            if future in wait_res.done:
                try:
                    res = future.result()
                    done_fail_branches['done'].append(temp_branch)
                    _log.info('DONE imported: %s %s %s', dx_analysis_id, temp_branch, temp_worktree_dir)
                except Exception as e:
                    _log.warning('Got exception when importing %s: %s; branch=%s, worktree=%s',
                                 dx_analysis_id, _format_exc(e), temp_branch, temp_worktree_dir)
                    done_fail_branches['fail'].append(temp_branch)

        for which in 'done', 'fail':
            with git_annex_tool._in_tmp_worktree(worktree_group_id=worktree_group_id+'/'+which, keep=True) as\
                 (summary_branch, summary_worktree):
                _log.info('SUMMARY: %d branches %s', len(done_fail_branches[which]), which)
                if done_fail_branches[which]:
                    git_annex_tool.execute_git(['merge',
                                                '-m', '"saving {} {} branches"'.format(len(done_fail_branches[which]),
                                                                                       which)] + done_fail_branches[which])

def parser_import_mult_dx_analyses(parser=argparse.ArgumentParser(fromfile_prefix_chars='@')):
    parser.add_argument('dx_analysis_ids', nargs='+', metavar='DX_ANALYSIS_ID', help='dnanexus analysis id(s)')
    parser.add_argument('--analysisDirPfx', dest='analysis_dir_pfx', default='pipelines/dxan-',
                        help='analysis dir prefix; analysis id will be added to it.')
    parser.add_argument('--setTmpDir', dest='set_tmp_dir', help='use this temp dir')
    util.cmd.common_args(parser, (('loglevel', None), ('version', None), ('tmp_dir', None)))
    util.cmd.attach_main(parser, import_mult_dx_analyses, split_args=True)
    return parser

__commands__.append(('import_mult_dx_analyses', parser_import_mult_dx_analyses))


# def _import_dx_analysis_one(dx_analysis_ids, analysis_dir_pfx):
#     """Import one or more DNAnexus analyses into git, in our analysis dir format."""
#     git_annex_tool = tools.git_annex.GitAnnexTool()
#     dnanexus_tool = tools.dnanexus.DxTool()
#     out = []
#     with git_annex_tool.batching() as git_annex_tool:
#         for dx_analysis_id in dx_analysis_ids:
#             try:
#                 out.append(_import_dx_analysis(dx_analysis_id, analysis_dir_pfx, git_annex_tool, dnanexus_tool))
#             except Exception as e:
#                 _log.warning('Could not import %s: %s', dx_analysis_id, e,
#                              ''.join(traceback.format_exception(type(e),
#                                                                 e, e.__traceback__))
#                              if hasattr(e, '__traceback__') else '')
                    
#     for analysis_dir, mdata, mdata_rel in out:
#         mdata = util.misc.transform_json_data(mdata, functools.partial(util.misc.maybe_wait_for_result, timeout=300))
#         mdata_rel = util.misc.transform_json_data(mdata_rel, functools.partial(util.misc.maybe_wait_for_result, timeout=300))
#         util.file.mkdir_p(analysis_dir)
#         _write_json(os.path.join(analysis_dir, 'metadata_orig.json'), **mdata)
#         _write_json(os.path.join(analysis_dir, 'metadata_with_gitlinks.json'), **mdata_rel)
#         run_inputs = copy.copy(mdata_rel['runInputs'])
#         run_inputs['_docker_img'] = mdata_rel['labels']['docker_img_hash']
#         run_inputs['_workflow_name'] = mdata_rel['workflowName']
#         _prepare_analysis_crogit_do(inputs=run_inputs,
#                                     analysis_dir=mdata_rel['labels']['analysis_dir'],
#                                     analysis_labels=mdata_rel['labels'], git_annex_tool=git_annex_tool)

# def import_dx_analyses_mult(dx_analysis_ids, analysis_dir_pfx):
#     git_annex_tool = tools.git_annex.GitAnnexTool()
#     with contextlib2.ExitStack() as stk:
        
#         with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(dx_analysis_ids)),
#                                                    util.misc.available_cpu_count()) as executor:
#             for dx_analysis_id in dx_analysis_ids:
#                 executor.submit(_import_dx_analysis_one, [dx_analysis_id], analysis_dir_pfx)
                                               
    


# def parser_import_dx_analyses_mult(parser=argparse.ArgumentParser(fromfile_prefix_chars='@')):
#     parser.add_argument('dx_analysis_ids', metavar='DX_ANALYSIS_ID', nargs='+', help='dnanexus analysis id(s)')
#     parser.add_argument('--analysisDirPfx', dest='analysis_dir_pfx', default='pipelines/dxan-',
#                         help='analysis dir prefix; analysis id will be added to it.')
#     util.cmd.attach_main(parser, import_dx_analyses_mult, split_args=True)
#     return parser

# __commands__.append(('import_dx_analyses_mult', parser_import_dx_analyses_mult))



# * submitting analyses
#
# Code that deals specifying sets of analyses to run, and submitting them to Cromwell.
#



# ** _get_workflow_inputs_spec

def _extract_wdl_from_docker_img(docker_img, analysis_dir):
    """Extracts from docker image the workflow and task .wdl files into the current directory"""
    _run('docker run --rm ' + docker_img + ' tar cf - source/pipes/WDL > wdl.tar', cwd=analysis_dir)
    _run('tar xvf wdl.tar', cwd=analysis_dir)
    util.file.mkdir_p(os.path.join(analysis_dir, 'tasks'))
    for f in glob.glob(os.path.join(analysis_dir, 'source/pipes/WDL/workflows/*.wdl')):
        _log.debug('copying %s to {}', f, analysis_dir)
        shutil.copy(f, analysis_dir)
    for f in glob.glob(os.path.join(analysis_dir, 'source/pipes/WDL/workflows/tasks/*.wdl')):
        _log.debug('copying %s to {}', f, analysis_dir)
        shutil.copy(f, analysis_dir)
        #shutil.copy(f, './tasks/')
    shutil.rmtree(os.path.join(analysis_dir, 'source'))
    os.unlink(os.path.join(analysis_dir, 'wdl.tar'))

def _get_workflow_inputs_spec(workflow_name, docker_img, analysis_dir):
    """Run womtool to get the inputs of the wdl workflow"""

    def _parse_input_spec(spec):
        optional = '(optional' in spec and spec.endswith(')')
        default = None
        opt_str = '(optional, default = '
        if opt_str in spec:
            default = _json_loads(spec[spec.rindex(opt_str)+len(opt_str):-1])
        return dict(optional=optional, default=default, spec=spec)

    input_spec_parsed = _run_get_json('womtool inputs ' + workflow_name + '.wdl', cwd=analysis_dir)
    _log.debug('input_spec_parsed=%s', input_spec_parsed.items())
    return _ord_dict(*[(input_name, _parse_input_spec(input_spec_str))
                       for input_name, input_spec_str in input_spec_parsed.items()])

# ** _construct_analysis_inputs_parser

# The code here is concerned with specifying a set of analyses to run.
# Simplest way is to explicitly specify all parameters for each analysis.  However, we often want to take a set of
# previously run analyses, and re-run them with specific modifications of parameters -- for exampe, with a newer version
# of viral-ngs.

# So, another way to specify a set of analyses is to point to a set of previously run analyses,
# and say "take each analysis in this set, modify it
# in a given way, and run it."
# Another is to split the parameter set into subsets, specify combinations of values for each subset, and then
# generate full parameter sets by taking a combination from each subset.

# *** parsing of analysis inputs specified in different ways

def _add_input_source(inp_dict, src):
    """For each input in `inp_dict`, add a corresponding key indicating its source as `src`.
    This is for tracking the provenance of inputs, e.g. when a parameter in one analysis is taken from another analysis,
    or from a particular file of parameters, or from the command line.
    """
    inp_dict_with_sources = copy.copy(inp_dict)
    for k in inp_dict:
        src_key = '_input_src.' + k
        util.misc.chk(src_key not in inp_dict)
        inp_dict_with_sources[src_key] = src
    return inp_dict_with_sources

def _analysis_inputs_from_name_value_pair(args, **kw):
    """Handle case of analysis inputs specified for one parameter as a list of one or more values on the command line."""
    def _maybe_quote(v): return '"'+v+'"' if args.quote else v
    return [_add_input_source({args.input_name: _json_loads(_maybe_quote(input_value))}, '__command_line__')
            for input_value in args.input_value]

def _apply_input_renamings(inps, input_name_subst, orig_workflow_name):
    """When basing a new analysis on an old one, sometimes we need to take an input from an old analysis but
    give it a new name.  This function takes care of this case."""
    for orig, repl in (input_name_subst or ()):
        for inp_name in tuple(inps):
            if inp_name == orig_workflow_name+'.'+orig:
                util.misc.chk(inp_name in inps)
                _log.info('RENAMING INPUT %s to %s', inp_name, inp_name.replace(orig, repl))
                _dict_rename_key(inps, inp_name, inp_name.replace(orig, repl))

def _analysis_inputs_from_analysis_dir_do(args, **kw):
    """Extract analysis inputs from an existing analysis dir.
    
    We take only inputs that were explicitly specified by the user for this analysis, not inputs that were filled in using
    the workflow's default values.
    """
    workflow_name = kw['workflow_name']
    mdata_fname = os.path.join(args.analysis_dir, 'metadata_with_gitlinks.json')
    mdata = _json_loadf(mdata_fname) 
    inps = mdata.get('runInputs', mdata['runInput'])

    # rename inputs, if needed
    # TODO: automatically match inputs to related workflows, based on the task that takes the inputs
    _apply_input_renamings(inps, args.input_name_subst, mdata['workflowName'])

    # change the workflow name to ours, if needed
    for inp_name in tuple(inps):
        if not inp_name.startswith(workflow_name+'.'):
            _log.info('FIXING INPUT %s', inp_name)
            wf_name = mdata['workflowName']
            util.misc.chk(inp_name.startswith(wf_name+'.'), 'bad inp name {}'.format(inp_name))
            inp_renamed=workflow_name+inp_name[inp_name.index('.'):]
            _dict_rename_key(inps, inp_name, inp_renamed)

    if _qry_json(mdata, 'labels.docker_img_hash'):
        inps['docker_img'] = _qry_json(mdata, 'labels.docker_img_hash')

    return _add_input_source(_make_git_links_absolute(inps, base_dir=os.path.abspath(args.analysis_dir)),
                             mdata['labels']['analysis_id'])

def _yield_analysis_dirs_under(analysis_dirs_roots, recurse=True):
    """Yield the analysis dirs under `analysis_dirs_roots`.  If `recurse` is False, only `analysis_dirs_roots`
    themselves are yielded.

    Note that, if new analysis dirs are added under the roots during iteration, some of them may be yielded;
    to return a list representing a snapshot of the analysis dirs at the current time, use
    _list_analysis_dirs_under() defined below.
    """
    dirs_seen = set()
    def _walk_error(e):
        raise e
    for root_dir in util.misc.make_seq(analysis_dirs_roots):
        for dirpath, subdirs, files in os.walk(root_dir, followlinks=True, onerror=_walk_error):
            dirpath = os.path.realpath(dirpath)
            if dirpath in dirs_seen:
                subdirs[:] = []
            elif is_analysis_dir(dirpath):
                yield dirpath
                subdirs[:] = []
            elif not recurse:
                break
            dirs_seen.add(dirpath)

def _get_analysis_dirs_under(analysis_dirs_roots, recurse=True):
    """Returns a tuple of the analysis dirs under `analysis_dirs_roots`, as they exist at the time of the call.
    If `recurse` is False, only `analysis_dirs_roots`themselves are yielded."""
    return tuple(_yield_analysis_dirs_under(analysis_dirs_roots, recurse=recurse))

def _analysis_inputs_from_analysis_dirs_roots(args, **kw):
    analysis_dirs_inputs = []
    analysis_dirs = list(_get_analysis_dirs_under(args.analysis_dirs_roots, recurse=args.recurse))
    _log.info('BBBBBBBBBBBB analysis_dirs({})={}'.format(len(analysis_dirs), analysis_dirs))
    for analysis_dir in analysis_dirs:
        if args.failed_only:
            mdata_fname = os.path.join(analysis_dir, 'metadata.json')
            mdata = _json_loadf(mdata_fname)
            if mdata['status'] == 'Success':
                continue
        if not os.path.isfile(os.path.join(analysis_dir, 'metadata_with_gitlinks.json')):
            continue

        args.analysis_dir = analysis_dir

        analysis_dirs_inputs.append(_analysis_inputs_from_analysis_dir_do(args, **kw))
    _log.info('AAAAAAAA analysis_dirs_inputs: {} {} {}'.format(args, kw, analysis_dirs_inputs))
    return analysis_dirs_inputs

def _construct_analysis_inputs_parser():
    parser = argparse.ArgumentParser(prefix_chars='+', prog='')
    subparsers = parser.add_subparsers(title='analysis inputs', description='specification of analysis inputs', dest='input_kind')

    parser_name_value_pair = subparsers.add_parser('nvp', help='name-value pair', prefix_chars='+')
    parser_name_value_pair.add_argument('input_name')
    parser_name_value_pair.add_argument('input_value', nargs='+', help='if multiple values given, analyses will be run with each')
    parser_name_value_pair.add_argument('++quote', '+q', default=False, action='store_true', help='quote the values')
    parser_name_value_pair.set_defaults(func=_analysis_inputs_from_name_value_pair)

    parser_analysis_dir = subparsers.add_parser('analysisDirsUnder',
                                                help='analysis dirs for past analyses', prefix_chars='+')
    parser_analysis_dir.add_argument('analysis_dirs_roots', metavar='ANALYSIS_DIR_ROOT', nargs='+')
    parser_analysis_dir.add_argument('++inputNameSubst', dest='input_name_subst', nargs=2, action='append',
                                    help='substitute strings in input names')
    parser_analysis_dir.add_argument('+r', '++recurse', default=False, action='store_true',
                                     help='include all analysis dirs under each root')
    parser_analysis_dir.add_argument('+f', '++failedOnly', dest='failed_only', default=False, action='store_true',
                                     help='only take analysis dirs for failed analyses')
    parser_analysis_dir.set_defaults(func=_analysis_inputs_from_analysis_dirs_roots)

    return parser


# ** _stage_inputs_for_backend

def _stage_inputs_for_backend(inputs, backend, skip_get=False):
    """Stage inputs with git links for backend"""
    def _stage_git_link(inp):
        if not _maps(inp, '$git_link'):
            return inp
        return _stage_file_for_backend(git_file_path=inp['$git_link'], backend=backend, skip_get=skip_get)
    return util.misc.transform_json_data(inputs, _stage_git_link)

def _normalize_cromwell_labels(labels):
    """Ensure that cromwell labels meet cromwell criteria"""
    labels = copy.copy(labels)
    MAX_LABEL_LEN = 255
    for k in tuple(labels):
        k_sfx = 0
        v = labels[k]
        if len(v) > MAX_LABEL_LEN:
            while v:
                labels[k + ('' if not k_sfx else '_{}'.format(k_sfx))] = v[:MAX_LABEL_LEN]
                v = v[MAX_LABEL_LEN:]
                k_sfx += 1
    return labels

def _parse_cromwell_output_str(cromwell_output_str):
    """Parse cromwell output"""
    assert cromwell_output_str.count('Final Outputs:') == 1
    json_beg = cromwell_output_str.index('Final Outputs:') + len('Final Outputs:')
    json_end = cromwell_output_str.index('\n}\n', json_beg) + 2
    return _json_loads(cromwell_output_str[json_beg:json_end])

# ** submit_analysis_wdl impl

def _submit_analysis_wdl_do(workflow_name, inputs,
                            analysis_dir_pfx,
                            analysis_labels=None,
                            cromwell_server_url='http://localhost:8000',
                            backend='Local',
                            prepare_only=False):
    """Submit a WDL analysis to a Cromwell server.

    Inputs to the analysis.
    The analysis can be executed on either a local or a cloud Cromwell backend.  This routine will, if needed,
    copy the inputs to the filesystem needed by the backend.

    Args:
        workflow_name: name of the workflow, from pipes/WDL/workflows
        inputs: inputs to the workflow; also, the docker image to use.
        analysis_dir_pfx: prefix for the analysis dir
        analysis_labels: json file specifying any analysis labels

    TODO:
        - option to ignore input workflow name, use only stage name
    """
    analysis_id = _create_analysis_id(workflow_name, prefix='analysis')
    _log.info('ANALYSIS_ID is %s', analysis_id)

    docker_img = inputs['docker_img']

    docker_img_hash = docker_img if re.search(r'@sha256:[0-9a-z]{64}\Z', docker_img) else \
        docker_img + '@' + _get_docker_hash(docker_img)

    analysis_dir = os.path.abspath(os.path.join(analysis_dir_pfx + ('-' if not analysis_dir_pfx.endswith('/') else '') + analysis_id))
    util.file.mkdir_p(analysis_dir)
    with util.file.pushd_popd(analysis_dir):
        _log.info('TTTTTTTTTTT analysis_dir=%s', analysis_dir)

        output_dir = os.path.join(analysis_dir, 'output')

        input_files_dir = os.path.join(analysis_dir, 'input_files')
        util.file.mkdir_p(input_files_dir)

        _extract_wdl_from_docker_img(docker_img_hash)
        workflow_inputs_spec = _get_workflow_inputs_spec(workflow_name, docker_img=docker_img_hash)
        _write_json('inputs-orig.json', **inputs)
        run_inputs = _make_git_links_relative(inputs, analysis_dir)

        input_sources = {k:v for k, v in run_inputs.items() if k.startswith('_input_src.')}

        run_inputs = _dict_subset(run_inputs, workflow_inputs_spec.keys())
        _write_json('inputs-git-links.json', **run_inputs)
        run_inputs_staged = _stage_inputs_for_backend(run_inputs, backend)
        _write_json('inputs.json', **run_inputs_staged)

        # TODO: use git annex batch mode to determine the keys for all the file-xxxx files, then
        # use batch mode to get the keys.  use -J to parallelize.  also option to use a cache remote, as
        # described at https://git-annex.branchable.com/tips/local_caching_of_annexed_files/

        # TODO: diff stages may have same-name inputs
        # note that dx runInput and originalInput mean the reverse of what one would think:
        # https://wiki.dnanexus.com/api-specification-v1.0.0/workflows-and-analyses

        ################# put in the right docker ID!!  and find a place to keep the docker cache.

        # TODO: option to update just some of the tasks.
        # actually, when compiling WDL, should have this option -- or, actually,
        # should make a new workflow where older apps are reused for stages that have not changed.
        docker_tool = tools.docker.DockerTool()
        _run('sed -i -- "s|{}|{}|g" *.wdl'.format('quay.io/broadinstitute/viral-ngs',
                                                  docker_tool.strip_image_hash(docker_img_hash)))

        util.file.mkdir_p(output_dir)
        util.file.mkdir_p(os.path.join(output_dir, 'outputs'))
        util.file.mkdir_p(os.path.join(output_dir, 'logs'))
        util.file.mkdir_p(os.path.join(output_dir, 'call_logs'))
        #util.file.mkdir_p(os.path.join(output_dir, 'metadata'))
        if backend == 'Local':
            wf_opts_dict = { "final_workflow_outputs_dir": os.path.join(output_dir, 'outputs'),
                             "final_workflow_log_dir": os.path.join(output_dir, 'logs'),
                             "final_call_logs_dir": os.path.join(output_dir, 'call_logs'),
                             "backend": "Local"
            }
        elif backend == 'JES':
            wf_opts_dict = {
                "final_workflow_log_dir": os.path.join(output_dir, 'logs'),
                "backend": "JES"
            }
        else:
            raise RuntimeError('Unknown backend - ' + backend)
        _write_json('cromwell_opts.json', **wf_opts_dict)
        _write_json('execution_env.json', ncpus=util.misc.available_cpu_count())


        analysis_labels = dict(
            input_sources,
            docker_img=docker_img,
            docker_img_hash=docker_img_hash,
            analysis_id=analysis_id,
            analysis_dir=analysis_dir,
            submitter=getpass.getuser(),
            **dict(analysis_labels or {}))
        _write_json('analysis_labels.json',
                    **_normalize_cromwell_labels(analysis_labels))

        # add cromwell labels: dx project, the docker tag we ran on, etc.

        _log.info('Validating workflow')
        _run('womtool', 'validate',  '-i',  'inputs.json', workflow_name + '.wdl')
        _log.info('Validated workflow; calling cromwell')
        _run('zip imports.zip *.wdl')
        for wdl_f in os.listdir('.'):
            if os.path.isfile(wdl_f) and wdl_f.endswith('.wdl') and wdl_f != workflow_name+'.wdl':
                os.unlink(wdl_f)

        if prepare_only:
            return

        try:
            cromwell_output_str = _run_get_output('cromwell', 'submit', workflow_name+'.wdl',
                                                  '-t', 'wdl', '-i', 'inputs.json', '-l', 'analysis_labels.json',
                                                  '-o', 'cromwell_opts.json',
                                                  '-p', 'imports.zip', '-h', cromwell_server_url)
            cromwell_returncode = 0
        except subprocess.CalledProcessError as called_process_error:
            cromwell_output_str = called_process_error.output
            cromwell_returncode = called_process_error.returncode

        _log.info('Cromwell returned with return code %d', cromwell_returncode)
        util.file.dump_file('cromwell_submit_output.txt', cromwell_output_str)
        _log.debug('cromwell output is %s', cromwell_output_str)

        if cromwell_returncode:
            raise RuntimeError('Cromwell failed - ' + cromwell_output_str)


def submit_analysis_wdl(workflow_name, inputs,
                        analysis_dir_pfx,
                        analysis_labels=None,
                        cromwell_server_url='http://localhost:8000',
                        backend='Local',
                        prepare_only=False):
    """Submit a WDL analysis (or a set of analyses) to a Cromwell server.

    Inputs to the analysis.
    The analysis can be executed on either a local or a cloud Cromwell backend.  This routine will, if needed,
    copy the inputs to the filesystem needed by the backend.

    Args:
        workflow_name: name of the workflow, from pipes/WDL/workflows
        inputs: inputs to the workflow; also, the docker image to use.
        analysis_dir_pfx: prefix for the analysis dir
        analysis_labels: json file specifying any analysis labels

    TODO:
        - option to ignore input workflow name, use only stage name
        - use cromwell's batch submit API to submit all analyses
        - submit initially in suspended state, then release?
    """

    analysis_labels = analysis_labels or collections.OrderedDict()
    analysis_labels['analysis_batch_id'] = _create_analysis_id(workflow_name, prefix='analyses_batch')
    analysis_labels['command_line'] = ' '.join(sys.argv)
    analysis_labels['submit_cwd'] = os.getcwd()

    def _proc(inp):
        return util.misc.make_seq(inp.func(inp, workflow_name=workflow_name),
                                  atom_types=collections.Mapping)
    n_submitted = 0
    for inps in itertools.product(*map(_proc, inputs)):
        _submit_analysis_wdl_do(workflow_name, _ord_dict_merge(inps), analysis_dir_pfx,
                                analysis_labels, cromwell_server_url,
                                backend, prepare_only=prepare_only)
        n_submitted += 1
    _log.info('{} analyses submitted.'.format(n_submitted))
    if n_submitted == 0:
        raise RuntimeError('No analyses submitted')


#########################

# ** submit_prepared_analysis

def _submit_prepared_analysis(analysis_dir,
                              cromwell_server_url='http://localhost:8000',
                              backend='Local',
                              processing_stats=collections.Counter()):
    """Submit prepared analysis.
    """
    cromwell_tool = tools.cromwell.CromwellTool()
    cromwell_server = CromwellServer(host=cromwell_server_url)
    with util.file.pushd_popd(analysis_dir):
        _log.info('TTTTTTTTTTT analysis_dir=%s', analysis_dir)
        if os.path.lexists('metadata_with_gitlinks.json'):
            _log.info('Analysis dir %s already completed, not submitting', analysis_dir)
            return
        if os.path.lexists('cromwell_submit_output.txt'):
            try:
                _run('git', 'annex', 'get', 'cromwell_submit_output.txt')
                cromwell_analysis_id = cromwell_tool.parse_cromwell_submit_output('cromwell_submit_output.txt')
                mdata = cromwell_server.get_metadata(cromwell_analysis_id)
                if mdata.get('status', None) in ('Running', 'Succeeded', 'Failed'):
                    _log.info('Analysis dir %s completed though not yet finalized; status=%s', analysis_dir, mdata['status'])
                    return
            except Exception:
                _log.info('Could not determine status for analysis dir %s', analysis_dir)
            # TODO: check that the analysis is in cromwell and that it is still pending or running

            _log.info('Resubmitting analysis dir: %s', analysis_dir)
            processing_stats['submitted_but_not_completed'] += 1
            for f in ('cromwell_submit_output.txt', 'cromwell_opts.json'):
                _run('git', 'annex', 'edit', f)
                os.remove(f)
        
        _run('git', 'annex', 'get', 'inputs-git-links.json')
        run_inputs = _json_loadf('inputs-git-links.json')

        input_sources = {k:v for k, v in run_inputs.items() if k.startswith('_input_src.')}
        run_inputs_staged = _stage_inputs_for_backend(run_inputs, backend)
        if os.path.lexists('inputs.json'):
            os.remove('inputs.json')
        util.misc.chk(not os.path.exists('inputs.json'), 'inputs.json should not yet exist')
        _write_json('inputs.json', **{k:v for k, v in run_inputs_staged.items() if not k.startswith('_')})

        if backend == 'Local':
            wf_opts_dict = { "backend": "Local", 'write_to_cache': True, 'read_from_cache': True
            }
        elif backend == 'JES':
            wf_opts_dict = {
                "backend": "JES"
            }
        else:
            raise RuntimeError('Unknown backend - ' + backend)
        if os.path.lexists('cromwell_opts.json'):
            os.remove('cromwell_opts.json')
        util.misc.chk(not os.path.exists('cromwell_opts.json'), 'cromwell_opts should not yet exist')
        _write_json('cromwell_opts.json', **wf_opts_dict)
        #_write_json('execution_env.json', ncpus=util.misc.available_cpu_count())

        # add cromwell labels: dx project, the docker tag we ran on, etc.

        # _log.info('Validating workflow')
        # _run('womtool', 'validate',  '-i',  'inputs.json', workflow_name + '.wdl')
        workflow_name = run_inputs['_workflow_name']
        try:
            cromwell_output_str = _run_get_output('cromwell', 'submit', workflow_name+'.wdl',
                                                  '-t', 'wdl', '-i', 'inputs.json', '-l', 'analysis_labels.json',
                                                  '-o', 'cromwell_opts.json',
                                                  '-p', 'imports.zip', '-h', cromwell_server_url)
            cromwell_returncode = 0
        except subprocess.CalledProcessError as called_process_error:
            cromwell_output_str = called_process_error.output
            cromwell_returncode = called_process_error.returncode

        _log.info('Cromwell returned with return code %d', cromwell_returncode)

        util.file.dump_file('cromwell_submit_output.txt', cromwell_output_str)
        time.sleep(.5)
        cromwell_analysis_id = cromwell_tool.parse_cromwell_submit_output('cromwell_submit_output.txt')
        util.misc.chk(cromwell_analysis_id, 'Cromwell analysis id not found in cromwell submit output')

        _log.debug('cromwell output is %s', cromwell_output_str)

        if cromwell_returncode:
            raise RuntimeError('Cromwell failed - ' + cromwell_output_str)

def parser_submit_prepared_analysis(parser=argparse.ArgumentParser()):
    parser.add_argument('analysis_dir')
    util.cmd.attach_main(parser, _submit_prepared_analysis, split_args=True)
    return parser

__commands__.append(('submit_prepared_analysis', parser_submit_prepared_analysis))


# ** submit_analysis_crogit

def _make_git_links_under_dir(d, base_dir, git_annex_tool, files_prefix='files'):
    """Change any git links within json struct `d` to be relative paths under `base_dir`."""

    base_dir = os.path.abspath(base_dir)
    def _make_git_link_point_under_dir(val, path):
        if not _maps(val, '$git_link'):
            return val
        _log.info('PROCESSING GIT LINK at %s: %s', path, val)
        path = list(functools.reduce(operator.concat, [str(p).split('.') for p in path], []))

        git_fname_arg = [files_prefix] + path + [os.path.basename(val.get('orig_path', val['$git_link']))]
        _log.info('git_fname_arg=%s', git_fname_arg)
        git_fname = os.path.join(*git_fname_arg)
        git_annex_tool.fromkey(key=val['git_annex_key'], fname=os.path.join(base_dir, git_fname), now=False)
        return dict(val, **{'$git_link': git_fname})
    return util.misc.transform_json_data(d, _make_git_link_point_under_dir)

def _submit_analysis_crogit_do(workflow_name, inputs,
                               analysis_dir_pfx,
                               analysis_labels,
                               git_annex_tool):
    """Submit a WDL analysis to a Cromwell server.

    Inputs to the analysis.
    The analysis can be executed on either a local or a cloud Cromwell backend.  This routine will, if needed,
    copy the inputs to the filesystem needed by the backend.

    Args:
        workflow_name: name of the workflow, from pipes/WDL/workflows
        inputs: inputs to the workflow; also, the docker image to use.
        analysis_dir_pfx: prefix for the analysis dir
        analysis_labels: json file specifying any analysis labels

    TODO:
        - option to ignore input workflow name, use only stage name
    """
    analysis_id = _create_analysis_id(workflow_name, prefix='analysis')
    _log.info('ANALYSIS_ID is %s', analysis_id)

    docker_img = inputs['docker_img']

    docker_img_hash = docker_img if re.search(r'@sha256:[0-9a-z]{64}\Z', docker_img) else \
        docker_img + '@' + _get_docker_hash(docker_img)

    analysis_dir = os.path.abspath(os.path.join(analysis_dir_pfx + ('-' if not analysis_dir_pfx.endswith('/') else '') + analysis_id))
    util.file.mkdir_p(analysis_dir)
    with util.file.pushd_popd(analysis_dir):
        _log.info('TTTTTTTTTTT analysis_dir=%s', analysis_dir)

        output_dir = os.path.join(analysis_dir, 'output')

        _extract_wdl_from_docker_img(docker_img_hash)
        workflow_inputs_spec = _get_workflow_inputs_spec(workflow_name, docker_img=docker_img_hash)
        _write_json('inputs-orig.json', **inputs)
        run_inputs = _make_git_links_under_dir(inputs, analysis_dir, git_annex_tool, files_prefix='files/runInputs')

        input_sources = {k:v for k, v in run_inputs.items() if k.startswith('_input_src.')}

        run_inputs = _dict_subset(run_inputs, workflow_inputs_spec.keys())
        _write_json('inputs-git-links.json', **run_inputs)

        # TODO: option to update just some of the tasks.
        # actually, when compiling WDL, should have this option -- or, actually,
        # should make a new workflow where older apps are reused for stages that have not changed.
        _run('sed -i -- "s|{}|{}|g" *.wdl'.format('quay.io/broadinstitute/viral-ngs',
                                                  tools.docker.DockerTool().strip_image_hash(docker_img_hash)))

        analysis_labels = dict(
            input_sources,
            docker_img=docker_img,
            docker_img_hash=docker_img_hash,
            analysis_id=analysis_id,
            analysis_dir=analysis_dir,
            submitter=getpass.getuser(),
            **dict(analysis_labels or {}))
        _write_json('analysis_labels.json',
                    **_normalize_cromwell_labels(analysis_labels))

        # add cromwell labels: dx project, the docker tag we ran on, etc.

        _run('zip imports.zip *.wdl')
        for wdl_f in os.listdir('.'):
            if os.path.isfile(wdl_f) and wdl_f.endswith('.wdl') and wdl_f != workflow_name+'.wdl':
                os.unlink(wdl_f)

def _get_full_inputs(workflow_name, inputs):
    """Given a parsed specification of a set of analysis inputs, construct a list of the full inputs to each analysis."""
    full_inps = []

    def _proc(inp):
        """Given a parsed specification of a set of partial analysis inputs, return a list of these partial analysis inputs."""
        return util.misc.make_seq(inp.func(inp, workflow_name=workflow_name),
                                  atom_types=collections.Mapping)
    for inps in itertools.product(*map(_proc, inputs)):
        full_inps.append(_ord_dict_merge(inps))
    return full_inps
    
def submit_analyses_crogit(workflow_name, inputs, analysis_labels=None, temp_worktree_base='../temp_worktrees'):
    """Submit a WDL analysis (or a set of analyses) to a crogit queue.

    Inputs to the analysis.

    Args:
        workflow_name: name of the workflow, from pipes/WDL/workflows
        inputs: inputs to the workflow; also, the docker image to use.
        analysis_labels: json file specifying any analysis labels

    TODO:
        - option to ignore input workflow name, use only stage name
    """

    crogit_batch_id = _create_analysis_id(workflow_name, prefix='analyses_batch')
    orig_cwd = os.getcwd()

    full_inps = _get_full_inputs(workflow_name, inputs)
    util.misc.chk(full_inps, "Error: no analyses specified")
    _log.info('%d analyses specified', len(full_inps))
    
    util.file.mkdir_p(os.path.join(temp_worktree_base, 'crogit', 'submitted'))

    analysis_labels = analysis_labels or collections.OrderedDict()
    analysis_labels['analysis_batch_id'] = crogit_batch_id
    analysis_labels['command_line'] = ' '.join(sys.argv)
    analysis_labels['submit_cwd'] = orig_cwd

    git_annex_tool = tools.git_annex.GitAnnexTool()
    with git_annex_tool.tmp_worktree(directory=temp_worktree_base,
                                     branch='/'.join(('crogit', 'submitted', crogit_batch_id)),
                                     start_branch=git_annex_tool.get_first_commit()) as tmp_worktree_dir:

        with git_annex_tool.batching() as git_annex_tool:
            for inps in full_inps:
                _submit_analysis_crogit_do(workflow_name=workflow_name, inputs=inps,
                                           analysis_dir_pfx=os.path.join(tmp_worktree_dir, 'crogit', 'submitted') + '/',
                                           analysis_labels=copy.deepcopy(analysis_labels), git_annex_tool=git_annex_tool)

        # and now git-annex-add all the things we created, and make a branch for this.
# end: def submit_analyses_crogit(workflow_name, inputs, analysis_labels=None, temp_worktree_base='../temp_worktrees')

def parser_submit_analyses_crogit(parser=argparse.ArgumentParser()):
    parser.add_argument('workflow_name', help='Workflow name')

    parser.add_argument('--inputs', dest='inputs', nargs='+', action=util.misc.NestedParserAction,
                        nested_parser=_construct_analysis_inputs_parser())
    parser.add_argument('--analysisLabels', dest='analysis_labels', nargs=2, action='append',
                        help='labels to attach to the analysis')
    util.cmd.attach_main(parser, submit_analyses_crogit, split_args=True)
    return parser

__commands__.append(('submit_analyses_crogit', parser_submit_analyses_crogit))

def _prepare_analysis_crogit_do(inputs,
                                analysis_dir,
                                analysis_labels,
                                git_annex_tool):
    """Prepare a WDL analysis for submission.

    Inputs to the analysis.
    The analysis can be executed on either a local or a cloud Cromwell backend.  This routine will, if needed,
    copy the inputs to the filesystem needed by the backend.

    Args:
        inputs: inputs to the workflow; special inputs specify docker image and workflow name.
        analysis_dir: where the prepared analysis goes
        analysis_labels: json file specifying any analysis labels

    """
    inputs = copy.copy(inputs)
    workflow_name = inputs['_workflow_name']
    analysis_id = _create_analysis_id(workflow_name, prefix='analysis')
    _log.info('ANALYSIS_ID is %s', analysis_id)

    docker_img = inputs['_docker_img']

    docker_img_hash = docker_img if re.search(r'@sha256:[0-9a-z]{64}\Z', docker_img) else \
        docker_img + '@' + _get_docker_hash(docker_img)

    inputs['_docker_img'] = docker_img_hash

    util.file.mkdir_p(analysis_dir)

    _log.info('TTTTTTTTTTT analysis_dir=%s', analysis_dir)

    _extract_wdl_from_docker_img(docker_img_hash, analysis_dir)
    workflow_inputs_spec = _get_workflow_inputs_spec(workflow_name, docker_img=docker_img_hash, analysis_dir=analysis_dir)
    _write_json(os.path.join(analysis_dir, 'inputs-orig.json'), **inputs)
    run_inputs = _make_git_links_under_dir(inputs, analysis_dir, git_annex_tool, files_prefix='files/runInputs')

    input_sources = {k:v for k, v in run_inputs.items() if k.startswith('_input_src.')}

    run_inputs = _dict_subset(run_inputs, set(workflow_inputs_spec.keys()) | set(k for k in run_inputs if k.startswith('_')))
    _write_json(os.path.join(analysis_dir, 'input-spec.json'), **workflow_inputs_spec)
    _write_json(os.path.join(analysis_dir, 'inputs-git-links.json'), **run_inputs)

    # TODO: option to update just some of the tasks.
    # actually, when compiling WDL, should have this option -- or, actually,
    # should make a new workflow where older apps are reused for stages that have not changed.
    docker_img_no_hash = tools.docker.DockerTool().strip_image_hash(docker_img_hash)
    _run('sed -i -- "s|{}|{}|g" {}/*.wdl'.format('quay.io/broadinstitute/viral-ngs', docker_img_no_hash, analysis_dir))
    _run('sed -i -- "s|{}|{}|g" {}/*.wdl'.format('viral-ngs_version_unknown',
                                                 docker_img_no_hash.split(':')[1], analysis_dir))

    analysis_labels = dict(
        input_sources,
        docker_img=docker_img,
        docker_img_hash=docker_img_hash,
        analysis_id=analysis_id,
        analysis_dir=analysis_dir,
        submitter=getpass.getuser())
    analysis_labels.update(dict(analysis_labels or {}))
    _write_json(os.path.join(analysis_dir, 'analysis_labels.json'),
                **_normalize_cromwell_labels(analysis_labels))

    # add cromwell labels: dx project, the docker tag we ran on, etc.

    _log.info('Validating workflow')
    run_inputs_staged_local = _stage_inputs_for_backend(run_inputs, backend='Local', skip_get=True)
    _write_json(os.path.join(analysis_dir, 'inputs-local.json'),
                **{k:v for k, v in run_inputs_staged_local.items() if not k.startswith('_')})
    _run('womtool', 'validate',  '-i',  'inputs-local.json', workflow_name + '.wdl', cwd=analysis_dir)
    _run('zip imports.zip *.wdl', cwd=analysis_dir)
    for wdl_f in os.listdir(analysis_dir):
        wdl_f = os.path.join(analysis_dir, wdl_f)
        if os.path.isfile(wdl_f) and wdl_f.endswith('.wdl') and os.path.basename(wdl_f) != workflow_name+'.wdl':
            os.unlink(wdl_f)

def _get_full_inputs(workflow_name, inputs):
    """Given a parsed specification of a set of analysis inputs, construct a list of the full inputs to each analysis."""
    full_inps = []

    def _proc(inp):
        """Given a parsed specification of a set of partial analysis inputs, return a list of these partial analysis inputs."""
        return util.misc.make_seq(inp.func(inp, workflow_name=workflow_name),
                                  atom_types=collections.Mapping)
    for inps in itertools.product(*map(_proc, inputs)):
        full_inps.append(_ord_dict_merge(inps))
    return full_inps
    
# def submit_analyses_crogit(workflow_name, inputs, analysis_labels=None, temp_worktree_base='../temp_worktrees'):
#     """Submit a WDL analysis (or a set of analyses) to a crogit queue.

#     Inputs to the analysis.

#     Args:
#         workflow_name: name of the workflow, from pipes/WDL/workflows
#         inputs: inputs to the workflow; also, the docker image to use.
#         analysis_labels: json file specifying any analysis labels

#     TODO:
#         - option to ignore input workflow name, use only stage name
#     """

#     crogit_batch_id = _create_analysis_id(workflow_name, prefix='analyses_batch')
#     orig_cwd = os.getcwd()

#     full_inps = _get_full_inputs(workflow_name, inputs)
#     util.misc.chk(full_inps, "Error: no analyses specified")
#     _log.info('%d analyses specified', len(full_inps))
    
#     util.file.mkdir_p(os.path.join(temp_worktree_base, 'crogit', 'submitted'))

#     analysis_labels = analysis_labels or collections.OrderedDict()
#     analysis_labels['analysis_batch_id'] = crogit_batch_id
#     analysis_labels['command_line'] = ' '.join(sys.argv)
#     analysis_labels['submit_cwd'] = orig_cwd

#     git_annex_tool = tools.git_annex.GitAnnexTool()
#     with git_annex_tool.tmp_worktree(directory=temp_worktree_base,
#                                      branch='/'.join(('crogit', 'submitted', crogit_batch_id)),
#                                      start_branch=git_annex_tool.get_first_commit()) as tmp_worktree_dir:

#         with git_annex_tool.batching() as git_annex_tool:
#             for inps in full_inps:
#                 _prepare_analysis_crogit_do(workflow_name=workflow_name, inputs=inps,
#                                             analysis_dir_pfx=os.path.join(tmp_worktree_dir, 'crogit', 'submitted') + '/',
#                                             analysis_labels=copy.deepcopy(analysis_labels), git_annex_tool=git_annex_tool)

#         # and now git-annex-add all the things we created, and make a branch for this.
# # end: def prepare_analyses_crogit(workflow_name, inputs, analysis_labels=None, temp_worktree_base='../temp_worktrees')

# def parser_prepare_analyses_crogit(parser=argparse.ArgumentParser()):
#     parser.add_argument('workflow_name', help='Workflow name')

#     parser.add_argument('--inputs', dest='inputs', nargs='+', action=util.misc.NestedParserAction,
#                         nested_parser=_construct_analysis_inputs_parser())
#     parser.add_argument('--analysisLabels', dest='analysis_labels', nargs=2, action='append',
#                         help='labels to attach to the analysis')
#     util.cmd.attach_main(parser, prepare_analyses_crogit, split_args=True)
#     return parser

# __commands__.append(('prepare_analyses_crogit', parser_prepare_analyses_crogit))

#########################



#########################


def _create_analysis_id(workflow_name, prefix):
    """Generate a unique ID for the analysis."""
    return util.file.string_to_file_name('-'.join(map(str, 
                                                       (prefix, time.strftime('%Y%m%d-%H%M%S', time.localtime())[2:], 
                                                        uuid.uuid4(), workflow_name))))[:1024]

def _get_docker_hash(docker_img):
    """Return a docker hash, given a docker tag."""
    try:
        _run('docker pull ' + docker_img)
    except Exception:
        _log.info('Retrying docker pull of %s', docker_img)
        _run('docker pull ' + docker_img)
        
    if docker_img.startswith('sha256:'):
        return docker_img
    digest_lines = _run_get_output('docker', 'images', '--digests', '--no-trunc', '--format',
                                   '{{.Repository}}:{{.Tag}} {{.Digest}}', _noquote('|'), 'grep',
                                   docker_img+(':' if ':' not in docker_img else '') + ' sha256:')
    digest_lines = [line for line in util.misc.maybe_decode(digest_lines).rstrip('\n').split('\n') if docker_img in line]
    assert len(digest_lines) == 1
    digest_line = digest_lines[0]
    _log.debug('digest_line is |||{}|||'.format(digest_line))
    img, digest = digest_line.strip().split()
    assert img == docker_img + (':latest' if ':' not in docker_img else '')
    assert digest.startswith('sha256:') and len(digest) == 71
    return digest

########################################################################################################################

def _is_prepared_analysis_dir(analysis_dir):
    """Check whether analysis dir has been set up for analysis"""
    return all(os.path.isfile(os.path.join(analysis_dir, f)) or
               os.path.islink(os.path.join(analysis_dir, f)) for f in ('inputs-git-links.json', 'imports.zip'))

########################################################################################################################

# ** Bringing benchmarks results in sync with benchmarks spec

#
# The code below takes a benchmark spec file, which specifies benchmarks and variants;
# and makes progress towards ensuring that, for each benchmark and each variant, we have computed a result of
# running that benchmark variant.
#

# *** generate_benchmark_variant_dirs

def _generate_benchmark_variant(benchmarks_spec_dir, benchmark_dir, benchmark_variant_name, benchmark_variant_def, git_annex_tool):
    """Generate a run-ready analysis dir for the given benchmark and benchmark variant."""

    analysis_dir = os.path.join(benchmark_dir, 'benchmark_variants', benchmark_variant_name)
    if _is_prepared_analysis_dir(analysis_dir):
        _log.info('Benchmark variant already generated, skipping: %s', analysis_dir)
        return
    util.file.mkdir_p(analysis_dir)
    run_inputs = _json_loadf(os.path.join(benchmarks_spec_dir, benchmark_dir, 'inputs-git-links.json'))
    metadata = _json_loadf(os.path.join(benchmarks_spec_dir, benchmark_dir, 'metadata_with_gitlinks.json')) \
        if os.path.isfile(os.path.join(benchmarks_spec_dir, benchmark_dir, 'metadata_with_gitlinks.json')) else {}
    run_inputs = _qry_json(json_data=dict(run_inputs=run_inputs, metadata=metadata),
                           jmespath_expr=benchmark_variant_def)
    _prepare_analysis_crogit_do(inputs=run_inputs, analysis_dir=analysis_dir, analysis_labels={}, git_annex_tool=git_annex_tool)

def generate_benchmark_variant_dirs(benchmarks_spec_file, one_benchmark_dir=None, one_benchmark_variant=None):
    """The code below takes a benchmark spec file, which specifies benchmarks (as analysis dirs) and variants,
    and generates, under each benchmark dir and for each variant, an analysis spec obtained by
    overriding the benchmark settings with the variant.

    Args:
      benchmarks_spec: a yaml file specifying benchmarks and variants.
    """

    benchmarks_spec_file = os.path.abspath(benchmarks_spec_file)
    benchmarks_spec_dir = os.path.dirname(benchmarks_spec_file)
    benchmarks_spec = util.misc.load_config(benchmarks_spec_file)
    _log.info('benchmarks_spec=%s', benchmarks_spec)

    benchmark_dirs = [one_benchmark_dir] if one_benchmark_dir else \
        list(map(functools.partial(os.path.relpath, start=benchmarks_spec_dir),
                 _get_analysis_dirs_under(benchmarks_spec['benchmark_dirs_roots'])))
    _log.info('benchmark_dirs=%s', benchmark_dirs)

    git_annex_tool = tools.git_annex.GitAnnexTool()
    with git_annex_tool.batching() as git_annex_tool:
        for benchmark_dir in benchmark_dirs:
            for benchmark_variant_name, benchmark_variant_def in benchmarks_spec['benchmark_variants'].items():
                if one_benchmark_variant and benchmark_variant_name != one_benchmark_variant: continue
                _generate_benchmark_variant(benchmarks_spec_dir, benchmark_dir, benchmark_variant_name,
                                            benchmark_variant_def, git_annex_tool)

def parser_generate_benchmark_variant_dirs(parser=argparse.ArgumentParser()):
    parser.add_argument('benchmarks_spec_file', help='benchmarks spec in yaml')
    parser.add_argument('--oneBenchmarkDir', dest='one_benchmark_dir', help='process only this benchmark dir')
    parser.add_argument('--oneBenchmarkVariant', dest='one_benchmark_variant', help='process only this benchmark variant')
    util.cmd.attach_main(parser, generate_benchmark_variant_dirs, split_args=True)
    return parser

__commands__.append(('generate_benchmark_variant_dirs', parser_generate_benchmark_variant_dirs))

def _generate_one_benchmark_variant_and_commit(benchmarks_spec_file, one_benchmark_dir, one_benchmark_variant,
                                               script, temp_worktree_dir):
    #util.file.mkdir_p(os.path.join(temp_worktree_dir, 'tmp'))
    _log.info('GEN_ONE: temp_worktree_dir=%s one_benchmark_dir=%s one_benchmark_variant=%s',
              temp_worktree_dir, one_benchmark_dir, one_benchmark_variant)
    log_fname = os.path.join(temp_worktree_dir, one_benchmark_dir, 'benchmark_variants', one_benchmark_variant,
                             'gen_benchmark_variant.log')
    util.file.mkdir_p(os.path.dirname(log_fname))
    try:
        with open(log_fname + '.out.txt', 'wt') as _out_stdout, open(log_fname + '.err.txt', 'wt') as _out_stderr:
            _run(script, 'generate_benchmark_variant_dirs', benchmarks_spec_file, '--oneBenchmarkDir', one_benchmark_dir,
                 '--oneBenchmarkVariant', one_benchmark_variant, cwd=temp_worktree_dir, stdout=_out_stdout, stderr=_out_stderr)
    finally:
        _run('git', 'annex', 'add', '.', cwd=temp_worktree_dir)
        _run('git', 'commit', '-m', 'generated benchmark variant {} {}'.format(one_benchmark_dir, one_benchmark_variant),
             cwd=temp_worktree_dir)

def _set_up_worktree_mult(one_benchmark_dir_and_one_benchmark_variant, exit_stack, git_annex_tool, worktree_group_id):
    return one_benchmark_dir_and_one_benchmark_variant + \
        exit_stack.enter_context(git_annex_tool._in_tmp_worktree(worktree_group_id=worktree_group_id,
                                                                 chdir=False))

def generate_mult_benchmark_variant_dirs(benchmarks_spec_file):
    """Generate multiple benchmark variant dirs from spec file."""

    benchmarks_spec_file = os.path.abspath(benchmarks_spec_file)
    benchmarks_spec_dir = os.path.dirname(benchmarks_spec_file)
    benchmarks_spec = util.misc.load_config(benchmarks_spec_file)
    _log.info('benchmarks_spec=%s', benchmarks_spec)

    benchmark_dirs = _get_analysis_dirs_under(benchmarks_spec['benchmark_dirs_roots'])
    _log.info('benchmark_dirs=%s', benchmark_dirs)

    tasks = []
    git_annex_tool = tools.git_annex.GitAnnexTool()
    with git_annex_tool.batching() as git_annex_tool:
        for benchmark_dir in benchmark_dirs:
            benchmark_dir = os.path.relpath(benchmark_dir)
            for benchmark_variant_name, benchmark_variant_def in benchmarks_spec['benchmark_variants'].items():
                analysis_dir = os.path.join(benchmark_dir, 'benchmark_variants', benchmark_variant_name)
                if _is_prepared_analysis_dir(analysis_dir):
                    _log.info('Benchmark variant already generated, skipping: %s', analysis_dir)
                    continue
                tasks.append((benchmark_dir, benchmark_variant_name))
    _log.info('TASKS: %d', len(tasks))
    if not tasks:
        return
    _log.info('Submitting %d tasks', len(tasks))

    git_annex_tool = tools.git_annex.GitAnnexTool()
    with contextlib2.ExitStack() as exit_stack:
        executor = exit_stack.enter_context(concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tasks),
                                                                                                  util.misc.available_cpu_count())))
        worktree_group_id = 'genvars_{:06d}'.format(random.randint(0,999999))
        temps = list(executor.map(functools.partial(_set_up_worktree_mult, exit_stack=exit_stack, git_annex_tool=git_annex_tool,
                                                    worktree_group_id=worktree_group_id+'/gen'), tasks))
        script = os.path.join(util.version.get_project_path(), os.path.basename(__file__))
        util.misc.chk(os.path.isabs(script) and os.path.samefile(script, __file__))
        futures = [executor.submit(_generate_one_benchmark_variant_and_commit,
                                   benchmarks_spec_file=benchmarks_spec_file, one_benchmark_dir=benchmark_dir,
                                   one_benchmark_variant=benchmark_variant,
                                   script=script, temp_worktree_dir=temp_worktree_dir)
                   for benchmark_dir, benchmark_variant, temp_branch, temp_worktree_dir in temps]
        wait_res = concurrent.futures.wait(futures)
        done_fail_branches = {'done': [], 'fail': []}
        for future, (benchmark_dir, benchmark_variant_name, temp_branch, temp_worktree_dir) in zip(futures, temps):
            if future in wait_res.done:
                try:
                    res = future.result()
                    done_fail_branches['done'].append(temp_branch)
                    _log.info('DONE generated: %s %s %s', benchmark_dir+'/'+benchmark_variant_name, temp_branch, temp_worktree_dir)
                except Exception as e:
                    _log.warning('Got exception when importing %s: %s; branch=%s, worktree=%s',
                                 benchmark_dir+'/'+benchmark_variant_name, _format_exc(e), temp_branch, temp_worktree_dir)
                    done_fail_branches['fail'].append(temp_branch)

        for which in 'done', 'fail':
            with git_annex_tool._in_tmp_worktree(worktree_group_id=worktree_group_id+'/'+which, keep=True) as\
                 (summary_branch, summary_worktree):
                _log.info('SUMMARY: %d branches %s', len(done_fail_branches[which]), which)
                if done_fail_branches[which]:
                    git_annex_tool.execute_git(['merge',
                                                '-m', '"saving {} {} branches"'.format(len(done_fail_branches[which]),
                                                                                       which)] + done_fail_branches[which])

def parser_generate_mult_benchmark_variant_dirs(parser=argparse.ArgumentParser(fromfile_prefix_chars='@')):
    parser.add_argument('benchmarks_spec_file', help='benchmarks spec in yaml')
    util.cmd.common_args(parser, (('loglevel', None), ('version', None), ('tmp_dir', None)))
    util.cmd.attach_main(parser, generate_mult_benchmark_variant_dirs, split_args=True)
    return parser

__commands__.append(('generate_mult_benchmark_variant_dirs', parser_generate_mult_benchmark_variant_dirs))


# *** submit_benchmark_variant_dirs

def submit_benchmark_variant_dirs(benchmarks_spec_file):
    """The code below takes a benchmark spec file, which specifies benchmarks (as analysis dirs) and variants,
    and generates, under each benchmark dir and for each variant, an analysis spec obtained by
    overriding the benchmark settings with the variant.

    Args:
      benchmarks_spec: a yaml file specifying benchmarks and variants.
    """

    benchmarks_spec_file = os.path.abspath(benchmarks_spec_file)
    benchmarks_spec_dir = os.path.dirname(benchmarks_spec_file)
    benchmarks_spec = util.misc.load_config(benchmarks_spec_file)
    _log.info('benchmarks_spec=%s', benchmarks_spec)

    benchmark_dirs = _get_analysis_dirs_under(benchmarks_spec['benchmark_dirs_roots'])
    _log.info('benchmark_dirs=%s', benchmark_dirs)

    processing_stats = collections.Counter()

    for benchmark_dir in benchmark_dirs:
        for benchmark_variant_name, benchmark_variant_def in benchmarks_spec['benchmark_variants'].items():
            _submit_prepared_analysis(analysis_dir=os.path.join(benchmark_dir, 'benchmark_variants', benchmark_variant_name),
                                      processing_stats=processing_stats)

def parser_submit_benchmark_variant_dirs(parser=argparse.ArgumentParser()):
    parser.add_argument('benchmarks_spec_file', help='benchmarks spec in yaml')
    util.cmd.attach_main(parser, submit_benchmark_variant_dirs, split_args=True)
    return parser

__commands__.append(('submit_benchmark_variant_dirs', parser_submit_benchmark_variant_dirs))

# ** cmp_benchmark_variants
def cmp_benchmark_variants(benchmarks_spec_file, variants, metric):
    """Print a report from comparing two benchmarks.
    """

    benchmarks_spec_file = os.path.abspath(benchmarks_spec_file)
    benchmarks_spec_dir = os.path.dirname(benchmarks_spec_file)
    benchmarks_spec = util.misc.load_config(benchmarks_spec_file)
    _log.info('benchmarks_spec=%s', benchmarks_spec)

    benchmark_dirs = _get_analysis_dirs_under(benchmarks_spec['benchmark_dirs_roots'])
    _log.info('benchmark_dirs=%s', benchmark_dirs)

    deltas = []

    for benchmark_dir in benchmark_dirs:
        variant_analysis_dirs = [os.path.join(benchmark_dir, 'benchmark_variants', benchmark_variant_name)
                                 for benchmark_variant_name in variants]
        mdatas = [os.path.join(d, 'metadata_with_gitlinks.json') for d in variant_analysis_dirs]
        if all(map(os.path.isfile, mdatas)):
            mdatas = list(map(_json_loadf, mdatas))
            delta = mdatas[1]['outputs'].get(metric, 0) - mdatas[0]['outputs'].get(metric, 0)
            if abs(delta) > 50:
                deltas.append((delta,) + tuple(variant_analysis_dirs))

    print('\n' + '\n'.join(map(str, sorted(deltas))))
    

def parser_cmp_benchmark_variants(parser=argparse.ArgumentParser()):
    parser.add_argument('benchmarks_spec_file', help='benchmarks spec in yaml')
    parser.add_argument('variants', nargs=2, help='the names of the variants')
    parser.add_argument('metric', help='the metric')
    util.cmd.attach_main(parser, cmp_benchmark_variants, split_args=True)
    return parser

__commands__.append(('cmp_benchmark_variants', parser_cmp_benchmark_variants))

# ** generate_benchmark_variant_comparisons

def _gather_one_benchmark_variant_stats(benchmark_dir_and_benchmark_variant):
    benchmark_dir, benchmark_variant = benchmark_dir_and_benchmark_variant
    """Load stats for one variant of one benchmark, as a pandas.Series"""
    result = []
    analysis_dir = os.path.join(benchmark_dir, 'benchmark_variants', benchmark_variant)
    if os.path.isfile(os.path.join(analysis_dir, 'metadata_with_gitlinks.json')):
        mdata = _load_analysis_metadata(analysis_dir, git_links_abspaths=True)
        mdata_flat = _flatten_analysis_metadata(mdata)
        for k, v in mdata_flat.items():
            if k.startswith('inputs.') or k.startswith('outputs.') or k.startswith('labels.') or k.startswith('status'):
                result.append((k, benchmark_variant, benchmark_dir, v))
    return result

def gather_benchmark_variant_stats(benchmarks_spec_file):
    """Gather benchmark variant stats into one place.  For each benchmark variant, we produce a file containing
    the stats for that variant across many benchmarks.
    """

    benchmarks_spec_file = os.path.abspath(benchmarks_spec_file)
    benchmarks_spec_dir = os.path.dirname(benchmarks_spec_file)
    benchmarks_spec = util.misc.load_config(benchmarks_spec_file)
    _log.info('benchmarks_spec=%s', benchmarks_spec)
    
    #_run('git', 'annex', 'get', '--include=metadata_with_gitlinks.json', *benchmarks_spec['benchmark_dirs_roots'])
    benchmark_dirs = sorted([os.path.relpath(d) for d in _get_analysis_dirs_under(benchmarks_spec['benchmark_dirs_roots'])
                             if os.path.isdir(os.path.join(d, 'benchmark_variants'))])
    _log.info('benchmark_dirs=%s', benchmark_dirs)

    processing_stats = collections.Counter()
    benchmark_variants = tuple(benchmarks_spec['benchmark_variants'].keys())

    with concurrent.futures.ThreadPoolExecutor(max_workers=util.misc.available_cpu_count()) as executor:
        map_res = sorted(functools.reduce(operator.concat,
                                          executor.map(_gather_one_benchmark_variant_stats,
                                                       itertools.product(benchmark_dirs,
                                                                         benchmark_variants)),
                                         []))
        r = collections.OrderedDict()
        for k, benchmark_variant, benchmark_dir, v in map_res:
            r.setdefault((k, benchmark_variant), collections.OrderedDict())[benchmark_dir] = v

        df = pd.DataFrame(r)
    # for benchmark_dir, 

    # for benchmark_dir in benchmark_dirs:
    #     for benchmark_variant in benchmark_variants:
    #         analysis_dir = os.path.join(benchmark_dir, 'benchmark_variants', benchmark_variant)
    #         mdata = _load_analysis_metadata(analysis_dir, git_links_abspaths=True)
    #         mdata_flat = _flatten_analysis_metadata(mdata)
    #         for k, v in mdata_flat.items():
    #             if k.startswith('inputs.') or k.startswith('outputs.') or k.startswith('labels.') or k.startswith('status'):
    #                 idx2stat.setdefault((benchmark_variant, k), collections.OrderedDict())[benchmark_dir] = v
    # df = pd.DataFrame(idx2stat)
    df.to_pickle('cmp/df.pkl.gz')

def parser_gather_benchmark_variant_stats(parser=argparse.ArgumentParser()):
    parser.add_argument('benchmarks_spec_file', help='benchmarks spec in yaml')
    util.cmd.attach_main(parser, gather_benchmark_variant_stats, split_args=True)
    return parser

__commands__.append(('gather_benchmark_variant_stats', parser_gather_benchmark_variant_stats))



def generate_benchmark_variant_comparisons(benchmarks_spec_file):
    """Generate/update reports of benchmark comparisons.
    """

    benchmarks_spec_file = os.path.abspath(benchmarks_spec_file)
    benchmarks_spec_dir = os.path.dirname(benchmarks_spec_file)
    benchmarks_spec = util.misc.load_config(benchmarks_spec_file)
    _log.info('benchmarks_spec=%s', benchmarks_spec)
    
    for d in benchmarks_spec['benchmark_dirs_roots']:
        _run('git', 'annex', 'get', '--include=metadata_with_gitlinks.json', d)
    benchmark_dirs = [d for d in _get_analysis_dirs_under(benchmarks_spec['benchmark_dirs_roots'])
                      if os.path.isdir(os.path.join(d, 'benchmark_variants'))]
    _log.info('benchmark_dirs=%s', benchmark_dirs)

    variant_pairs = [(variant_1, variant_2)
                     for variant_1, variant_2s in benchmarks_spec.get('compare_variants', {}).items() for variant_2 in variant_2s]
    _log.info('variant_paris=%s', variant_pairs)
    processing_stats = collections.Counter()

    muscle_tool = tools.muscle.MuscleTool()

    cmp_output_dir = benchmarks_spec['compare_output_dir']
    util.file.mkdir_p(cmp_output_dir)

    org = util.misc.Org()
    org.directive('TITLE', 'Benchmark comparisons')
    org.text('')
    with org.headline('Comparisons: created {}'.format(datetime.datetime.now().strftime("%I:%M%p on %B %d, %Y"))):
        for variants in variant_pairs:
            with org.headline('={}= vs ={}='.format(variants[0], variants [1])):
                for metric in benchmarks_spec['compare_metrics']:
                    with org.headline('Metric: ={}='.format(metric)):

                        deltas = []
                        no_change = 0
                        total = 0

                        for benchmark_dir in benchmark_dirs:
                            variant_analysis_dirs = [os.path.join(benchmark_dir, 'benchmark_variants', benchmark_variant_name)
                                                     for benchmark_variant_name in variants]
                            mdatas_fnames = [os.path.join(d, 'metadata_with_gitlinks.json') for d in variant_analysis_dirs]
                            sample_name = ''
                            if all(map(os.path.lexists, mdatas_fnames)):
                                _log.info('Both exist: %s', mdatas_fnames)
                                mdatas = list(map(_json_loadf, mdatas_fnames))
                                for mdata in mdatas:
                                    contigs_fasta = str(_qry_json(mdata, 'outputs."assemble_denovo.assemble.contigs_fasta"."$git_link"'))
                                    if contigs_fasta.endswith('fasta'):
                                        sample_name_here = os.path.basename(contigs_fasta).split('.')[0]
                                        util.misc.chk(not sample_name or sample_name_here == sample_name)
                                        sample_name = sample_name_here
                                        mdata['labels']['sample_name'] = sample_name
                                util.misc.chk(mdatas[0]['status'] == 'Failed' or metric in mdatas[0]['outputs'],
                                              'no {} in {}'.format(metric, variant_analysis_dirs[0]))
                                util.misc.chk(mdatas[1]['status'] == 'Failed' or metric in mdatas[1]['outputs'],
                                              'no {} in {}'.format(metric, variant_analysis_dirs[1]))
                                delta = mdatas[1]['outputs'].get(metric, 0) - mdatas[0]['outputs'].get(metric, 0)
                                total += 1
                                if delta == 0:
                                    no_change += 1
                                if abs(delta) > 10:
                                    deltas.append((delta, tuple(variant_analysis_dirs), sample_name))
                            else:
                                _log.info('skipping %s: %s', benchmark_dir, mdatas_fnames)

                        org.text('Total: {}.  No change: {}.'.format(total, no_change))
                        org.text('')

                        deltas_series = pd.Series(map(operator.itemgetter(0), deltas))
                        deltas_series.hist(bins=20)
                        fn = os.path.join(cmp_output_dir, 'fig{}.svg'.format(random.randint(0,10000)))
                        pp.savefig(fn)
                        org.text('[[file:{}]]'.format(os.path.basename(fn)))
                                
                        for delta_val, delta_infos in (): # itertools.groupby(sorted(deltas), key=operator.itemgetter(0)):
                            delta_infos = list(delta_infos)
                            with org.headline('{} (x{})'.format(delta_val, len(delta_infos))):
                                _log.info('delta_infos=%s', delta_infos)
                                for delta, variant_analysis_dirs, sample_name in delta_infos:
                                    t = ' '.join('[[{}][{}]] '.format(variant_analysis_dir, 'ab'[i])
                                                 for i, variant_analysis_dir in enumerate(variant_analysis_dirs))
                                    with org.headline(sample_name + ' ' + t):
                                        _diff_analyses_org(analysis_dirs=variant_analysis_dirs, org=org)

        cmp_output_fname = os.path.join(cmp_output_dir, 'index.org')
        util.file.dump_file(cmp_output_fname, str(org))
        _run('emacs -Q --batch --eval \'(progn (find-file "{}") (org-html-export-to-html))\''.format(cmp_output_fname))

def parser_generate_benchmark_variant_comparisons(parser=argparse.ArgumentParser()):
    parser.add_argument('benchmarks_spec_file', help='benchmarks spec in yaml')
    util.cmd.attach_main(parser, generate_benchmark_variant_comparisons, split_args=True)
    return parser

__commands__.append(('generate_benchmark_variant_comparisons', parser_generate_benchmark_variant_comparisons))

def generate_benchmark_variant_comparisons_from_gathered_stats(benchmarks_spec_file):
    """Generate/update reports of benchmark comparisons.
    """

    benchmarks_spec_file = os.path.abspath(benchmarks_spec_file)
    benchmarks_spec_dir = os.path.dirname(benchmarks_spec_file)
    benchmarks_spec = util.misc.load_config(benchmarks_spec_file)
    _log.info('benchmarks_spec=%s', benchmarks_spec)
    
    variant_pairs = [(variant_1, variant_2)
                     for variant_1, variant_2s in benchmarks_spec.get('compare_variants', {}).items() for variant_2 in variant_2s]
    _log.info('variant_paris=%s', variant_pairs)
    processing_stats = collections.Counter()

    muscle_tool = tools.muscle.MuscleTool()

    cmp_output_dir = benchmarks_spec['compare_output_dir']
    util.file.mkdir_p(cmp_output_dir)

    benchmark_stats = pd.read_pickle('cmp/df.pkl.gz')

    org = util.misc.Org()
    org.directive('TITLE', 'Benchmark comparisons')
    org.text('')
    with org.headline('Comparisons: created {}'.format(datetime.datetime.now().strftime("%I:%M%p on %B %d, %Y"))):
        for variants in variant_pairs:
            with org.headline('={}= vs ={}='.format(variants[0], variants [1])):
                for metric in benchmarks_spec['compare_metrics']:
                    with org.headline('Metric: ={}='.format(metric)):

                        no_change = 0
                        total = 0

                        deltas = benchmark_stats[metric][variants[0]] - benchmark_stats[metric][variants[1]]
                        _log.info('deltas=%s', deltas)

                        org.text('Total: {}.  No change: {}.'.format(total, no_change))
                        org.text('')

                        deltas_series = deltas.dropna()
                        deltas_series.hist(bins=20)
                        fn = os.path.join(cmp_output_dir, 'fig{}.svg'.format(random.randint(0,10000)))
                        pp.savefig(fn)
                        org.text('[[file:{}]]'.format(os.path.basename(fn)))
                                
                        for delta_val, delta_infos in (): # itertools.groupby(sorted(deltas), key=operator.itemgetter(0)):
                            delta_infos = list(delta_infos)
                            with org.headline('{} (x{})'.format(delta_val, len(delta_infos))):
                                _log.info('delta_infos=%s', delta_infos)
                                for delta, variant_analysis_dirs, sample_name in delta_infos:
                                    t = ' '.join('[[{}][{}]] '.format(variant_analysis_dir, 'ab'[i])
                                                 for i, variant_analysis_dir in enumerate(variant_analysis_dirs))
                                    with org.headline(sample_name + ' ' + t):
                                        _diff_analyses_org(analysis_dirs=variant_analysis_dirs, org=org)

        cmp_output_fname = os.path.join(cmp_output_dir, 'index.org')
        util.file.dump_file(cmp_output_fname, str(org))
        _run('emacs -Q --batch --eval \'(progn (find-file "{}") (org-html-export-to-html))\''.format(cmp_output_fname))

def parser_generate_benchmark_variant_comparisons_from_gathered_stats(parser=argparse.ArgumentParser()):
    parser.add_argument('benchmarks_spec_file', help='benchmarks spec in yaml')
    util.cmd.attach_main(parser, generate_benchmark_variant_comparisons_from_gathered_stats, split_args=True)
    return parser

__commands__.append(('generate_benchmark_variant_comparisons_from_gathered_stats',
                     parser_generate_benchmark_variant_comparisons_from_gathered_stats))


# ** print_analysis_stats
def print_analysis_stats(analysis_dirs_roots, qry_expr):
    """Print analysis stats for given analyses"""
    analysis_dirs = analysis_dirs_roots # _get_analysis_dirs_under(analysis_dirs_roots)
    analyses = []
    for analysis_dir in analysis_dirs:
        mdata_fname = os.path.join(analysis_dir, 'metadata_with_gitlinks.json')
        if os.path.isfile(mdata_fname):
            analyses.append(_json_loadf(mdata_fname))
    print(_qry_json(json_data=analyses, jmespath_expr=qry_expr))

def parser_print_analysis_stats(parser=argparse.ArgumentParser()):
    parser.add_argument('qry_expr', help='jmespath expr')
    parser.add_argument('analysis_dirs_roots', nargs='+', help='analysis dirs roots')
    
    util.cmd.attach_main(parser, print_analysis_stats, split_args=True)
    return parser

__commands__.append(('print_analysis_stats', parser_print_analysis_stats))





########################################################################################################################

def parser_submit_analysis_wdl(parser=argparse.ArgumentParser()):
    parser.add_argument('workflow_name', help='Workflow name')

    parser.add_argument('--inputs', dest='inputs', nargs='+', action=util.misc.NestedParserAction,
                        nested_parser=_construct_analysis_inputs_parser())
    parser.add_argument('--analysisDirPfx', dest='analysis_dir_pfx', default='pipelines/an',
                        help='directory where analysis will be stored; a unique suffix will be added')
    parser.add_argument('--analysisLabels', dest='analysis_labels', nargs=2, action='append',
                        help='labels to attach to the analysis')
    parser.add_argument('--backend', default='Local', help='backend on which to run')
    parser.add_argument('--prepareOnly', dest='prepare_only', default=False, action='store_true',
                        help='set up analysis but do not submit to cromwell')
    util.cmd.attach_main(parser, submit_analysis_wdl, split_args=True)
    return parser

__commands__.append(('submit_analysis_wdl', parser_submit_analysis_wdl))


########################################################################################################################


# * finalize_analysis_dir

# ** CromwelServer

class CromwellServer(object):

    """Interactions with a running Cromwell server"""

    def __init__(self, host):
        self.host = host

    def _api(self, endpoint, query=()):
        query_str = ('?' + urlencode(query)) if query else ''
        return _run_get_json('curl', '-s', '-X', 'GET', '{}/api/workflows/v1/{}{}'.format(self.host, endpoint, query_str),
                             '-H', 'accept: application/json')

    def get_workflows(self, query=()):
        return self._api(endpoint='query', query=query)['results']
    
    def get_metadata(self, workflow_id):
        return self._api('{}/metadata?expandSubWorkflows=false'.format(workflow_id))

# end: class CromwellServer(object)

def _is_analysis_done(analysis_dir):
    return os.path.exists(os.path.join(analysis_dir, 'output', 'logs'))

def _record_file_metadata(val, analysis_dir, root_dir):
    """If 'val' is a filename, return a dict representing the file and some metadata about it;
    otherwise, return val as-is."""
    if isinstance(val, list): return [_record_file_metadata(v, analysis_dir, root_dir) for v in val]
    if _is_mapping(val): return collections.OrderedDict([(k, _record_file_metadata(v, analysis_dir, root_dir))
                                                          for k, v in val.items()])
    if not (_is_str(val) and (os.path.isfile(val) or os.path.isdir(val))): return val
    file_info = collections.OrderedDict([('_is_file' if os.path.isfile(val) else '_is_dir', True)])
    assert val.startswith(analysis_dir) or val.startswith(root_dir), \
        '{} does not start with {} or {}'.format(val, analysis_dir, root_dir)
    if val.startswith(analysis_dir):
        relpath = os.path.relpath(val, analysis_dir)
        abspath = os.path.join(analysis_dir, relpath)
    else:
        cromwell_executions_dir = os.path.dirname(os.path.dirname(root_dir))
        relpath = os.path.relpath(val, cromwell_executions_dir)
        abspath = os.path.join(analysis_dir, 'output',
                               'call_logs' if os.path.basename(val) in ('stdout', 'stderr') else 'outputs', relpath)
        if os.path.isfile(val) and not os.path.isfile(abspath):
            _log.debug('LINKING {} to {}'.format(val, abspath))
            util.file.mkdir_p(os.path.dirname(abspath))
            shutil.copy(val, abspath)
        if os.path.isdir(val):
            util.file.mkdir_p(abspath)

    assert os.path.isabs(abspath) and abspath.startswith(analysis_dir), \
        'bad abspath: {} analysis_dir: {}'.format(abspath, analysis_dir)
    relpath = os.path.relpath(abspath, analysis_dir)
    assert not os.path.isabs(relpath), 'should be relative: {}'.format(relpath)
    assert os.path.isfile(abspath) or os.path.isdir(abspath), 'not file or dir: {}'.format(abspath)
    assert os.path.isdir(abspath) or os.path.getsize(abspath) == os.path.getsize(val)
    file_info['relpath'] = relpath
    if os.path.isfile(abspath):
        file_info['size'] = os.path.getsize(abspath)
        file_info['md5'] = _run_get_output('md5sum ' + abspath).strip().split()[0]
    return file_info

def is_analysis_dir(d):
    """Test whether a given directory is an analysis dir"""
    return all(os.path.isfile(os.path.join(d, f)) for f in ('metadata_with_gitlinks.json',)) or \
        all(os.path.isfile(os.path.join(d, f)) for f in ('inputs-git-links.json',))

def _gather_analysis_dirs(analysis_dirs_roots, processing_stats):
    def _get_analysis_dirs(d):
        if not os.path.isdir(d): return []
        if not os.access(d, os.R_OK | os.X_OK):
            processing_stats['dir_skipped_no_access'] += 1
            return []
        if is_analysis_dir(d):
            return [d]
        return functools.reduce(operator.concat, [_get_analysis_dirs(os.path.join(d, subd))
                                                  for subd in os.listdir(d)], [])
    return functools.reduce(operator.concat, map(_get_analysis_dirs, util.misc.make_seq(analysis_dirs_roots)), [])

# ** finalize_analysis_dirs impl

# *** _gather_fmdata_from_calls
def _gather_fmdata_from_calls(analysis_metadata, lcpath2fmdata=None):
    """For lcpaths in `analysis_metadata`, gather fmdata.
    """

    chk, make_seq, first_non_None = util.misc.from_module(util.misc, 'chk make_seq first_non_None')

    # var: lcpath2fmdata - maps file path string (as used in workflow metadata, either local or cloud path)
    #    to a dict mapping metadata item name (md5, size, etc) to value.
    lcpath2fmdata = first_non_None(lcpath2fmdata, collections.defaultdict(dict))

    # Extract md5s of files from callCaching data
    # Note: the exact API used here may not be fully official/permanent -
    # https://github.com/broadinstitute/cromwell/issues/4498
    # The code here should be written to work even if the API changes.
    for call_name, call_attempts in analysis_metadata['calls'].items():
        for call_attempt in call_attempts:
            for call_attempts_field, hashes_field in (('inputs', 'input'), ('outputs', 'output expression')):
                # 'inp' within this loop refers to inputs on first pass and to outputs on the second pass
                inp2hashes = {}
                file_type_inputs = set()
                inp_type_and_name_to_hashes = _qry_json(call_attempt,
                                                        'callCaching.hashes."{}"'.format(hashes_field), {})
                #print(inp_type_and_name_to_hashes)
                for inp_type_and_name, inp_hashes in inp_type_and_name_to_hashes.items():
                    inp_type, inp_name = inp_type_and_name.split()
                    if inp_type == 'File':
                        file_type_inputs.add(inp_name)
                        # actual input type might be File or Array[File]
                        chk(isinstance(inp_hashes, (_str_type, list)))
                        inp2hashes[inp_name] = make_seq(inp_hashes)

                inp_name_to_val = _qry_json(call_attempt, call_attempts_field, {})
                for inp_name, inp_val in inp_name_to_val.items():
                    if inp_name in file_type_inputs:
                        chk(isinstance(inp_val, (_str_type, list)))
                        inp_vals = make_seq(inp_val)
                        # print('inp_name=', inp_name, 'inp_val=', inp_val, 'inp_vals=', inp_vals, 
                        #       'inp2hashes=', inp2hashes)
                        chk(len(inp_vals) == len(inp2hashes[inp_name]))
                        for inp_one_val, inp_one_hash in zip(inp_vals, inp2hashes[inp_name]):
                            one_val_mdata = lcpath2fmdata[inp_one_val]
                            if _is_valid_md5(inp_one_hash):
                                one_val_mdata['md5'] = inp_one_hash
            # for call_attempts_field, hashes_field in (('inputs', 'input'), ('outputs', 'output expression'))
        # for call_attempt in call_attempts
    # for call_name, call_attempts in analysis_metadata['calls'].items()
    
    return lcpath2fmdata
# end: def _gather_fmdata_from_calls(analysis_metadata, lcpath2fmdata=None)

# *** _gather_file_metadata_from_analysis_metadata
def _gather_file_metadata_from_analysis_metadata(analysis_metadata, lcpath2fmdata=None, gcloud_tool=None):
    """For files referenced in `analysis_metadata` (as strings denoting local or cloud paths),
    gather file metadata such as md5 hashes and sizes.

    We can gather them from:
       - Cromwell's callCaching info
       - for files on gs, gsutil stat calls
       - for local files under git-annex control, the symlink into the annex which includes the md5 and file size
       - the local file itself
    """

    chk, make_seq, first_non_None = util.misc.from_module(util.misc, 'chk make_seq first_non_None')

    gcloud_tool = first_non_None(gcloud_tool, tools.gcloud.GCloudTool())

    # var: lcpath2fmdata - maps file path string (as used in workflow metadata, either local or cloud path)
    #    to a dict mapping metadata item name (md5, size, etc) to value.
    lcpath2fmdata = first_non_None(lcpath2fmdata, collections.defaultdict(dict))

    _gather_fmdata_from_calls(analysis_metadata, lcpath2fmdata)

    #
    # For inputs/output files for which we could not get an md5 from the callCaching metadata,
    # try to get it from other sources.
    #

    files_to_gs_stat = set()
    ga_tool = tools.git_annex.GitAnnexTool()
    for file_path in sorted(lcpath2fmdata):
        if file_path.startswith('gs://'):
            files_to_gs_stat.add(file_path)
        elif ga_tool.is_link_into_annex(file_path):
            ga_key_attrs = ga_tool.examinekey(ga_tool.lookupkey(file_path))
            for mdata_field in ('md5', 'size'):
                if mdata_field in ga_key_attrs:
                    lcpath2fmdata[mdata_field] = ga_key_attrs[mdata_field]

    if not gcloud_tool.is_anonymous_client():
        gs_mdata = gcloud_tool.get_metadata_for_objects(files_to_gs_stat)
        for gs_uri in files_to_gs_stat:
            for mdata_field in ('md5', 'size'):
                lcpath2fmdata[mdata_field] = gs_mdata[gs_uri][mdata_field]

#    for file_path in sorted(file_paths_in_analysis_metadata - set(file_path_to_metadata.keys())):
#        _log.info('NO MD5: %s', file_path)

#    print('RESULT-----------')
#    print('\n'.join(map(str, file_path_to_metadata.items())))
    return lcpath2fmdata

    # file_path_to_obj = _ord_dict()
    # dict_path_to_obj = _ord_dict()
    
    # def file_node_to_obj(val, path):

    #     if not _is_str(val):
    #         return val
    #     if val in file_path_to_obj:
    #         return file_path_to_obj[val]

    #     if val.startswith('gs://') or os.path.is_file(val) or _ga_tool().is_link_into_annex(f):
    #         file_path = val
    #         file_obj = _ord_dict(('_file', file_path))
    #         file_path_to_obj[file_path] = file_obj
    #         dict_path_to_obj[path] = file_obj
    #         return file_obj

    # analysis_metadata_with_file_objs = _transform_json_data(analysis_metadata, file_node_to_obj)

def finalize_analysis_dirs(cromwell_host, hours_ago=24, analysis_dirs_roots=None, status_only=False):
    """After a submitted cromwell analysis has finished, save results to the analysis dir.
    Save metadata, mark final workflow result, make paths relative to analysis dir."""
    cromwell_server = CromwellServer(host=cromwell_host)
    cromwell_tool = tools.cromwell.CromwellTool()
    processing_stats = collections.Counter()
    status_stats = collections.Counter()

    analysis_dirs = () if not analysis_dirs_roots else _get_analysis_dirs_under(analysis_dirs_roots)
    cromwell_analysis_id_to_dir = {}
    for analysis_dir in analysis_dirs:
        fname = os.path.join(analysis_dir, 'cromwell_submit_output.txt')
        _log.info('looking at cromwell output file %s; exists? %s', fname, os.path.isfile(fname))
        if os.path.isfile(fname):
            _log.info('looking at cromwell output file %s', fname)
            cromwell_analysis_id = cromwell_tool.parse_cromwell_submit_output(fname)
            cromwell_analysis_id_to_dir[cromwell_analysis_id] = analysis_dir
    _log.info('GOT ANALYSIS IDS %s', cromwell_analysis_id_to_dir)
    git_annex_tool = tools.git_annex.GitAnnexTool()
    with git_annex_tool.batching() as git_annex_tool:

        query = []
        if hours_ago:
            query.append(('submission', _isoformat_ago(hours=hours_ago)))
        if not status_only:
            query.extend([('status', 'Succeeded'), ('status', 'Failed')])
        for wf in cromwell_server.get_workflows(query=query):
            processing_stats['workflowsFromCromwell'] += 1
            mdata = cromwell_server.get_metadata(wf['id'])
            assert mdata['id'] == wf['id']
            assert 'workflowLog' not in mdata or mdata['workflowLog'].endswith('workflow.{}.log'.format(wf['id']))
            analysis_dir = mdata['labels'].get('analysis_dir', cromwell_analysis_id_to_dir.get(mdata['id'], None))
            _log.info('ID %s ADIR %s', mdata['id'], analysis_dir)
            if analysis_dir:
                if analysis_dirs_roots and not any(_is_under_dir(analysis_dir, analysis_dirs_root)
                                                   for analysis_dirs_root in analysis_dirs_roots):
                    processing_stats['notUnderAnalysisDirsRoots'] += 1
                    continue
                cromwell_output_file = os.path.join(analysis_dir, 'cromwell_submit_output.txt')
                if git_annex_tool.maybe_get(cromwell_output_file):
                    id_from_analysis_dir = cromwell_tool.parse_cromwell_submit_output(cromwell_output_file)
                    if id_from_analysis_dir != mdata['id']:
                        processing_stats['id from analysis dir does not match metadata id'] += 1
                        _log.info('MISMATCH: analysis_dir=%s id=%s id_from_dir=%s',
                                  analysis_dir, mdata['id'], id_from_analysis_dir)
                        continue
            else:
                processing_stats['noAnalysisDirForWorkflow'] += 1
                continue

            if status_only:
                _log.info('WORKFLOW: %s STATUS: %s', wf['id'], mdata['status'])
                status_stats[mdata['status']] += 1
                continue

            util.file.mkdir_p(analysis_dir)
            mdata_fname = os.path.join(analysis_dir, 'metadata_orig.json') # mdata['workflowLog'][:-4]+'.metadata.json'
            mdata_rel_fname = os.path.join(analysis_dir, 'metadata_with_gitlinks.json') # mdata['workflowLog'][:-4]+'.metadata.json'
            if os.path.lexists(mdata_rel_fname):
                processing_stats['metadata_already_saved'] += 1
            elif 'workflowRoot' not in mdata:
                processing_stats['workflow_root_not_in_mdata'] += 1
            else:
                if not os.path.lexists(mdata_fname):
                    _write_json(mdata_fname, **mdata)
                #mdata_rel = _record_file_metadata(mdata, analysis_dir, mdata['workflowRoot'])

                mdata['runInputs'] = util.misc.json_loads(mdata['submittedFiles']['inputs'])

                leaf_jpaths = util.misc.json_gather_leaf_jpaths(mdata)
                str_leaves = list(filter(_is_str, util.misc.map_vals(leaf_jpaths)))
                git_annex_tool.import_urls(str_leaves, ignore_non_urls=True, now=False)
                mdata_rel = _resolve_links_in_json_data(val=mdata, rel_to_dir=analysis_dir,
                                                        methods=[functools.partial(_resolve_link_using_gathered_filestat,
                                                                                   git_annex_tool=git_annex_tool),
#                                                                 _resolve_link_local_path,
#                                                                 _resolve_link_gs,
                                                        ], relpath='files')
                mdata_rel = util.misc.transform_json_data(mdata_rel, functools.partial(util.misc.maybe_wait_for_result, timeout=300))

                _write_json(mdata_rel_fname, **mdata_rel)
                _log.info('Wrote metadata to %s and %s', mdata_fname, mdata_rel_fname)
                processing_stats['saved_metata'] += 1
    _log.info('Processing stats: %s', str(processing_stats))
    _log.info('Status stats: %s', str(status_stats))

def parser_finalize_analysis_dirs(parser=argparse.ArgumentParser()):
    parser.add_argument('--cromwellHost', dest='cromwell_host', default='localhost:8000', help='cromwell server hostname')
    parser.add_argument('--hoursAgo', dest='hours_ago', type=int, default=24,
                        help='only consider workflows submitted this or fewer hours ago')
    parser.add_argument('--analysisDirsRoots', dest='analysis_dirs_roots', nargs='+',
                        help='only consider analyses whose analysis dirs are in one of these trees')
    parser.add_argument('--statusOnly', dest='status_only', default=False, action='store_true', help='print status and quit')
    util.cmd.attach_main(parser, finalize_analysis_dirs, split_args=True)
    return parser

__commands__.append(('finalize_analysis_dirs', parser_finalize_analysis_dirs))

########################################################################################################################

def unfold_git_links(json_fnames):
    """Given a json file with git_links, ensure that all of them point to a file."""
    git_annex_tool = tools.git_annex.GitAnnexTool()
    with git_annex_tool.batching() as git_annex_tool:
        fname2key = {}

        def unfold_one_link(val, path, json_dir):
            if not (_maps(val, '$git_link') and _maps(val, 'git_annex_key')):
                return val
            util.misc.chk(not os.path.isabs(val['$git_link']))
            fname = os.path.join(json_dir, val['$git_link'])
            key = val['git_annex_key']

            if fname in fname2key:
                util.misc.chk(fname2key[fname] == key)
            else:
                git_annex_tool.fromkey(key=key,
                                       fname=fname, now=False)

        for json_fname in json_fnames:
 
            json_data = _json_loadf(json_fname)
            json_dir = os.path.dirname(os.path.abspath(json_fname))
            util.misc.transform_json_data(json_data, node_handler=functools.partial(unfold_one_link, json_dir=json_dir))

def parser_unfold_git_links(parser=argparse.ArgumentParser()):
    parser.add_argument('json_fnames', nargs='+', metavar='JSON_FNAME', help='names of files to unfold')
    util.cmd.attach_main(parser, unfold_git_links, split_args=True)
    return parser

__commands__.append(('unfold_git_links', parser_unfold_git_links))

########################################################################################################################


# * import_from_url


def _standardize_url(url):
    if url.startswith(DX_URI_PFX):
        return _standardize_dx_url(url)
    return url

def _minimize_url(url):
    if url.startswith(DX_URI_PFX):
        url = DX_URI_PFX+_url_to_dxid(url)
    return url


_url2md5key = {}

def _get_url_md5_key(url_key):
    if url_key not in _url2md5key:
        alt_keys = [k for k in _git_annex_get_metadata(key=url_key, field='alt_keys')
                    if k.startswith('MD5')]
        if alt_keys:
            _url2md5key[url_key] = alt_keys[0]
    return _url2md5key.get(url_key, None)

def _set_url_md5_key(url_key, md5_key):
    if url_key in _url2md5key:
        assert _url2md5key[url_key] == md5_key
        return
    _url2md5key[url_key] = md5_key
    _git_annex_set_metadata(key=url_key, field='alt_keys', val=md5_key)
    

def import_from_url(url, git_file_path, fast=False, import_dirs=False):
    """Imports a URL into the git-annex repository."""

    url = _standardize_url(url)
    if os.path.isdir(git_file_path):
        git_file_path = os.path.join(git_file_path, os.path.basename(url))

    #assert not os.path.lexists(git_file_path)
    if os.path.lexists(git_file_path):
        # TODO: keep link if it's correct
        # TODO: have a --force option to do this
        # TODO: check what if this is a dir
        os.unlink(git_file_path)

    # check if we have an md5 key for the url.
    url_key = _git_annex_get_url_key(url)
    md5_key = _get_url_md5_key(url_key)

    if not md5_key and url.startswith('gs://'):
        gs_stat_result = _gs_stat(url)
        _log.debug('stat result: %s', gs_stat_result)
        if _maps(gs_stat_result, 'md5'):
            # print(gs_stat_result['Content-Length:'], type(gs_stat_result['Content-Length:']))
            # print(gs_stat_result['md5'], type(gs_stat_result['md5']))
            # print(str(uritools.urisplit(url).path), type(str(uritools.urisplit(url).path)))
                  
            md5_key = 'MD5E-s' + gs_stat_result['Content-Length:'] + '--' \
                      + gs_stat_result['md5'] + os.path.splitext(str(uritools.urisplit(url).path))[1]
            _run('git annex setpresentkey', md5_key, _get_gs_remote_uuid(), 1)
            _run('git annex registerurl', md5_key, url)
        elif not import_dirs:
            _log.info('Not importing dir: %s', url)
            return
        else:
            util.file.mkdir_p(git_file_path)
            for gs_entry in _gs_ls(url):
                import_from_url(gs_entry, git_file_path, fast=fast, import_dirs=import_dirs)
            return

    if md5_key:
        _run('git', 'annex', 'fromkey', '--force', md5_key, git_file_path)
        if not fast:
            _run('git', 'annex', 'get', git_file_path)
        return git_file_path

    _run('git', 'annex', 'addurl', '--fast' if fast else None, '--file', git_file_path, url)
    if not fast:
        _set_url_md5_key(url_key=url_key, md5_key=_git_annex_lookupkey(git_file_path))

    return git_file_path
    # check that the state of a dx object is closed.
    # (have option to) still compute md5.  if we don't write to disk, it'll be fast?

def parser_import_from_url(parser=argparse.ArgumentParser()):
    parser.add_argument('url', help='the URL of the file to add.')
    parser.add_argument('--gitFilePath', dest='git_file_path', help='filename in the git-annex repository', default='.')
    parser.add_argument('--fast', default=False, action='store_true', help='do not immediately download the file')
    parser.add_argument('--importDirs', dest='import_dirs', default=False, action='store_true', help='import directories')
    util.cmd.attach_main(parser, import_from_url, split_args=True)
    return parser

__commands__.append(('import_from_url', parser_import_from_url))

########################################################################################################################

def _gs_stat(gs_url):
    assert gs_url.startswith('gs://')
    try:
        stat_output = _run_get_output('gsutil', 'stat', gs_url)
        result = {}
        for line in stat_output.split('\n')[1:]:
            result[line[:25].strip()] = line[25:].strip()

        if 'Hash (md5):' in result:
            result['md5'] = binascii.hexlify(result['Hash (md5):'].decode('base64'))

        return result

    except subprocess.CalledProcessError:
        return {}

def _copy_to_gs(git_file_path, gs_prefix = 'gs://sabeti-ilya-cromwell'):   # TODO make gs prefix configurable
    """Ensure the given file exists in gs."""

    if gs_prefix.endswith('/'):
        gs_prefix = gs_prefix[:-1]
    whereis = _run_get_json('git annex whereis --json ' + git_file_path, cwd=os.path.dirname(os.path.abspath(git_file_path)))
    locs = []
    assert whereis['success']
    urls = [url for w in whereis['whereis'] for url in w.get('urls', ()) if url.startswith(gs_prefix) and _gs_stat(url)]
    dx_urls = [url for w in whereis['whereis'] for url in w.get('urls', ()) if url.startswith(DX_URI_PFX)]
    fname = os.path.basename(git_file_path)
    urls_with_right_fname = [url for url in urls if url.endswith(fname)]

    key = _git_annex_lookupkey(git_file_path)
    gs_remote_uuid = _get_gs_remote_uuid()

    size_from_key = None
    if key.startswith('MD5E-s'):
        size_from_key = int(key.split('-')[1][1:])
        md5_from_key = key.split('--')[1][:32]
        md5_base64_from_key = binascii.unhexlify(md5_from_key).encode('base64').strip()

    if not urls_with_right_fname:
        gs_url = '/'.join((gs_prefix, 'inp', _md5_base64(whereis['key']), fname))
        headers = None
        # TODO: set Content-Type based on extension
        # TODO: maybe use -z to compress if large text file.

        # TODO: if have a dx URL, use the Storage Transfer Service to download directly from 
        # dx and avoid transfer charges, if we're not running on google here.
        # or, run a wdl workflow with the temp url as parameter.

        if urls:
            _run('gsutil cp', urls[0], gs_url)
            _run('git annex registerurl', key, gs_url, cwd=os.path.dirname(os.path.abspath(git_file_path)))
        elif size_from_key is not None and (size_from_key is None or size_from_key > 1000) and _running_on_aws() and dx_urls:
            # TODO fix dx egress hack
            # avoid AWS egress charges by copying directly from dnanexus
            dx_tmp_url =  _dx_make_download_url(_url_to_dxid(dx_urls[0]))
            gs_url = tools.google.GCloudTool().transfer_to_gcs(dx_tmp_url, size_from_key, md5_base64_from_key)
            _run('git annex setpresentkey', key, gs_remote_uuid, 1, cwd=os.path.dirname(os.path.abspath(git_file_path)))
            _run('git annex registerurl', key, gs_url, cwd=os.path.dirname(os.path.abspath(git_file_path)))
        else:
            _git_annex_get(git_file_path)
            _run('gsutil cp', git_file_path, gs_url, cwd=os.path.dirname(os.path.abspath(git_file_path)))
            _run('git annex setpresentkey', key, gs_remote_uuid, 1, cwd=os.path.dirname(os.path.abspath(git_file_path)))
            _run('git annex registerurl', key, gs_url, cwd=os.path.dirname(os.path.abspath(git_file_path)))

        urls_with_right_fname.append(gs_url)
    assert _gs_stat(urls_with_right_fname[0])
    assert _git_annex_checkpresentkey(key, remote=gs_remote_uuid, git_file_path=git_file_path)
    return urls_with_right_fname[0]

def _stage_file_for_backend(git_file_path, backend, skip_get=False):
    """Ensure file exists in filesystem of the given Cromwell backend, and return the path to the file
    (local or cloud, depending on the backend)."""
    if backend == 'Local':
        if not skip_get:
            tools.git_annex.GitAnnexTool().get(git_file_path)
        return os.path.abspath(git_file_path)
    elif backend == 'JES':
        return _copy_to_gs(git_file_path)

########################################################################################################################

def _load_workflow_metadata(analysis_dir):
    return _json_loadf(glob.glob(os.path.join(analysis_dir, 'output', 'logs', 'workflow.*.json'))[0])

def analysis_dir_to_json(analysis_dir):
    """Gather data from one analysis dir into simple json"""
    mdata = _load_workflow_metadata(analysis_dir)

def gather_analyses(dirs):
    """Gather analyses from various sources into one simple uniform format."""
    pass

########################################################################################################################


# * compare_analysis_pairs

def _load_analysis_metadata(analysis_dir, git_links_abspaths=False):
    """Read the analysis metadata from an analysis dir.  If `git_links_abspath` is True,
    paths in git links will be made absolute."""
    _log.info('loading analysis metadata from %s', analysis_dir)
    mdata = _json_loadf(os.path.join(analysis_dir, 'metadata_with_gitlinks.json'))
    mdata['analysis_dir'] = analysis_dir
    if git_links_abspaths:
        mdata = _make_git_links_absolute(mdata, base_dir=os.path.abspath(analysis_dir))
    if not _qry_json(mdata, 'labels.sample_name'):
        contigs_fasta = str(_qry_json(mdata, 'outputs."assemble_denovo.assemble.contigs_fasta"."$git_link"'))
        if contigs_fasta.endswith('fasta'):
            sample_name = os.path.basename(contigs_fasta).split('.')[0]
            mdata.setdefault('labels', collections.OrderedDict())['sample_name'] = sample_name
    return mdata

def _flatten_analysis_metadata_list(val, pfx=''):
    """Convert analysis metadata to a flat list of key-value pairs"""
    if _is_git_link(val):
        util.misc.chk('git_annex_key' in val, 'no key: {} {}'.format(val, pfx))
        return _flatten_analysis_metadata_list(val['git_annex_key'], pfx)

    def _concat(iters): return list(itertools.chain.from_iterable(iters))

    if isinstance(val, list):
        return _concat(_flatten_analysis_metadata_list(v, pfx+('.' if pfx else '')+str(i))
                       for i, v in enumerate(val))
    if _is_mapping(val):
        return _concat(_flatten_analysis_metadata_list(v, pfx+('.' if pfx else '')+k)
                       for k, v in val.items())
    return [(pfx, val)]

def _key_matches_prefixes(key, key_prefixes):
    return not key_prefixes or any(key.startswith(key_prefix)
                                   for key_prefix in key_prefixes)

def _flatten_analysis_metadata(mdata, key_prefixes=()):
    #_log.info('mdata=%s', len(mdata))
    flat_mdata = _ord_dict(*_flatten_analysis_metadata_list(mdata))
    #_log.info('flat_mdata=%s', len(flat_mdata))
    if 'status' in flat_mdata:
        flat_mdata['succeeded'] = int(flat_mdata['status'] == 'Succeeded')
    if key_prefixes:
        flat_mdata = _ord_dict(*[(key, val) for key, val in flat_mdata.items()
                                 if key=='analysis_dir' or _key_matches_prefixes(key, key_prefixes)])
    return flat_mdata

def diff_analyses(analysis_dirs, key_prefixes=()):
    """Print differences between two analysis dirs."""
    assert len(analysis_dirs)==2, 'currently can only compare two analyses'

    flat_mdatas = [_flatten_analysis_metadata(_load_analysis_metadata(analysis_dir,
                                                                      git_links_abspaths=True),
                                              key_prefixes=key_prefixes)
                   for analysis_dir in analysis_dirs]
    all_keys = sorted(set(itertools.chain.from_iterable(flat_mdatas)))
    print('------------------------ diffs ----------------------------')
    for key in all_keys:
        if key.startswith('calls.'): continue
        vals = [flat_mdatas[i].get(key, None) for i in (0, 1)]
        if vals[0] != vals[1]:
            def _fmt(s):
                s = str(s)
                if len(s) > 120:
                    s = s[:120] + ' ...'
                return s
            print('KEY: ', key)
            print('   ', _fmt(vals[0]))
            print('   ', _fmt(vals[1]))

def _diff_analyses_org(analysis_dirs, org, key_prefixes=()):
    """Generate an org view of differences between two analysis dirs."""
    assert len(analysis_dirs)==2, 'currently can only compare two analyses'

    flat_mdatas = [_flatten_analysis_metadata(_load_analysis_metadata(analysis_dir,
                                                                      git_links_abspaths=True),
                                              key_prefixes=key_prefixes)
                   for analysis_dir in analysis_dirs]
    all_keys = sorted(set(itertools.chain.from_iterable(flat_mdatas)))

    for key in all_keys:
        if key.startswith('calls.'): continue
        if key.startswith('labels.') and not (key.startswith('labels.docker_img') or key.startswith('labels.sample_name')): continue
        if key in ('analysis_dir', 'end', 'id', 'start', 'submission', 'submittedFiles.inputs', 'submittedFiles.labels',
                   'workflowRoot', 'runInputs'): continue
        vals = [flat_mdatas[i].get(key, None) for i in (0, 1)]
        if vals[0] != vals[1]:
            def _fmt(s):
                s = str(s)
                if False and len(s) > 120:
                    s = s[:120] + ' ...'
                return s
            with org.headline('{}'.format(key)):
                org.text(_fmt(vals[0]))
                org.text(_fmt(vals[1]))

def parser_diff_analyses(parser=argparse.ArgumentParser()):
    parser.add_argument('analysis_dirs', nargs=2, help='the two analysis dirs')
    parser.add_argument('--keyPrefixes', dest='key_prefixes', nargs='+',
                        help='only consider metadata items starting with these prefixes')
    util.cmd.attach_main(parser, diff_analyses, split_args=True)
    return parser

__commands__.append(('diff_analyses', parser_diff_analyses))
    


def diff_jsons(jsons, key_prefixes=()):
    """Print differences between two analysis dirs."""
    assert len(jsons)==2, 'currently can only compare two analyses'
    mdatas = [_json_loadf(json_file) for json_file in jsons]
    mdatas[1] = mdatas[1]['workflow']

    flat_mdatas = [_flatten_analysis_metadata(mdata,
                                              key_prefixes=key_prefixes)
                   for mdata in mdatas]
    all_keys = sorted(set(itertools.chain.from_iterable(flat_mdatas)))
    print('------------------------ diffs ----------------------------')
    for key in all_keys:
        if key.startswith('calls.'): continue
        vals = [flat_mdatas[i].get(key, None) for i in (0, 1)]
        if vals[0] != vals[1]:
            def _fmt(s):
                s = str(s)
                if len(s) > 120:
                    s = s[:120] + ' ...'
                return s
            print('KEY: ', key)
            print('   ', _fmt(vals[0]))
            print('   ', _fmt(vals[1]))

def parser_diff_jsons(parser=argparse.ArgumentParser()):
    parser.add_argument('jsons', nargs=2, help='the two json files')
    parser.add_argument('--keyPrefixes', dest='key_prefixes', nargs='+',
                        help='only consider metadata items starting with these prefixes')
    util.cmd.attach_main(parser, diff_jsons, split_args=True)
    return parser

__commands__.append(('diff_jsons', parser_diff_jsons))
    


def compare_analysis_pairs(analysis_dirs_roots, common, filter_A, filter_B, label, metrics):
    """Compare pairs of analyses from `analysis_dirs` where  the first analysis of the pair matches the criteria in `filter_A`,
    the second matches the criteria in `filter_B`, and they both have the same value of `common`.
    For such pairs, show the distribution of output `metrics`.  All expressions are in jmespath.

    Terms:
       analysis group -- a group of analyses that agree on values in `common`
    """

    # For each distinct combination of `common_fields` values, gather the analyses with that combination.

    processing_stats = collections.Counter()

    mdatas = [_json_loadf(os.path.join(analysis_dir, 'metadata_with_gitlinks.json'))
              for analysis_dir in _get_analysis_dirs_under(analysis_dirs_roots)
              if os.path.isfile(os.path.join(analysis_dir, 'metadata_with_gitlinks.json'))]
    processing_stats['mdatas'] = len(mdatas)

    id2mdata = {mdata['id']:mdata for mdata in mdatas}
    id2common = {an_id:str(_qry_json(mdata, common)) for an_id, mdata in id2mdata.items()}
    _log.info('id2common=%d %s', len(id2common), list(id2common.items())[:10])
    common2ids = collections.defaultdict(list)
    for an_id, common_val in id2common.items():
        common2ids[common_val].append(an_id)
    metric2diffs = collections.defaultdict(list)
    for common, ids in common2ids.items():
        ids_A = [an_id for an_id in ids if _qry_json(id2mdata[an_id], filter_A)]
        ids_B = [an_id for an_id in ids if _qry_json(id2mdata[an_id], filter_B)]
        if not (ids_A and ids_B): continue
        processing_stats['ids_A_{}'.format(len(ids_A))] += 1
        processing_stats['ids_B_{}'.format(len(ids_B))] += 1
        if len(ids_A) > 1:
            _log.warning('MULTIPLE DIRS MATCH: ids_A={}'.format([_qry_json(id2mdata[id_A], label) for id_A in ids_A]))
        if len(ids_B) > 1:
            _log.warning('MULTIPLE DIRS MATCH: ids_B={}'.format([_qry_json(id2mdata[id_B], label) for id_B in ids_B]))

        id_A = ids_A[0]
        id_B = ids_B[0]
        for metric in metrics:
            metric2diffs[metric].append((_qry_json(id2mdata[id_A], metric) - _qry_json(id2mdata[id_B], metric),
                                         _qry_json(id2mdata[id_A], label), _qry_json(id2mdata[id_B], label)))

    for metric in (metrics or ()):
        _log.info('metric=%s diffs:', metric)
        for metric_delta, items in itertools.groupby(sorted(metric2diffs[metric]), operator.itemgetter(0)):
            items = list(items)
            _log.info('********** DELTA=%s: %d items ************', metric_delta, len(items))
            if metric_delta != 0:
                for item in items:
                    _log.info('item=%s', item)

    _log.info('processing_stats=%s', processing_stats)

def parser_compare_analysis_pairs(parser=argparse.ArgumentParser()):
    parser.add_argument('--analysisDirsRoots', dest='analysis_dirs_roots', nargs='+', required=True,
                        help='dir roots containing analysis dirs')
    parser.add_argument('--common', required=True)
    parser.add_argument('--filterA', dest='filter_A', required=True)
    parser.add_argument('--filterB', dest='filter_B', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--metrics', action='append')
    # parser.add_argument('--keyPrefixes', dest='key_prefixes', nargs='+',
    #                     default=['inputs.', 'outputs.', 'labels.docker_img_hash', 'status', 'succeeded'],
    #                     help='only consider metadata items starting with these prefixes')
    # parser.add_argument('--commonKeysPrefixes', dest='common_keys_prefixes', nargs='+',
    #                     help='only compare pairs of analyses that agree on keys starting with these prefixes')
    util.cmd.attach_main(parser, compare_analysis_pairs, split_args=True)
    return parser

__commands__.append(('compare_analysis_pairs', parser_compare_analysis_pairs))


# * transform_json

def _transform_json(json_file, jmespath_expr):
    """Transform a parsed json structure using jmespath.
    """
    if json_file.upper().endswith('.YAML'):
        with open(json_file) as json_f:
            json_data = yaml.safe_load(json_f) or {}
    else:
        json_data = _json_loadf(json_file)
    print(_pretty_print_json(_qry_json(json_data=json_data, jmespath_expr=jmespath_expr)))

def parser_transform_json(parser=argparse.ArgumentParser()):
    parser.add_argument('json_file', help='json file to import')
    parser.add_argument('jmespath_expr', help='jmespath expr',
                        nargs='?')
    util.cmd.attach_main(parser, _transform_json, split_args=True)
    return parser

__commands__.append(('transform_json', parser_transform_json))

# ** analyze_workflows_orig

def analyze_workflows_orig():
    bam2analyses = collections.defaultdict(list)
    stats = collections.Counter()
    for analysis in os.listdir('runs'):
        def _file(name): return os.path.join('runs', analysis, name)
        if not os.path.isfile(_file('cromwell_execution_metadata.json')):
            stats['no_metadata'] += 1
            continue
        mdata = _json_loadf(_file('cromwell_execution_metadata.json'))
        if not mdata['workflowName'].startswith('assemble_denovo'):
            stats['wrong_workflowName'] += 1
            continue
        if not mdata['status'] == 'Succeeded':
            stats['wrong_status'] += 1
            continue
        stats['considered'] += 1
        mdata = copy.deepcopy(mdata)
        mdata['analysis_id'] = analysis
        bam = mdata['inputs']['assemble_denovo.reads_unmapped_bam']
        bam2analyses[bam].append(mdata)
        print(analysis)
    print(stats)
    diffs = []
    for bam, mdatas in bam2analyses.items():
        if len(mdatas) > 1:
            print('bam=', bam, 'analyses=', '\n'.join(map(str, [mdata['analysis_id'] for mdata in mdatas])))

            def canonicalize_val(v, analysis_id):
                if not _is_str(v): return v
                orig_v = v
                v = os.path.join('runs', analysis_id, v)
                if _is_str(v) and os.path.islink(v) and os.path.lexists(v) and '.git/annex/objects' in os.path.realpath(v):
                    return os.path.basename(os.path.realpath(v))
                #print('not canon: ', v)
                return orig_v

            for i in range(len(mdatas)):
                for j in range(i+1, len(mdatas)):
                    if mdatas[i]['outputs']['assemble_denovo.refine_2x_and_plot.assembly_length_unambiguous'] == \
                       mdatas[j]['outputs']['assemble_denovo.refine_2x_and_plot.assembly_length_unambiguous']: continue
                    print('COMPARING ', mdatas[i]['analysis_id'], mdatas[j]['analysis_id'])
                    def show_diff(which):
                        def get_items(mdata):
                            analysis_id = mdata['analysis_id']
                            mdata = mdata[which]
                            mdata = {k: canonicalize_val(v, analysis_id) for k, v in mdata.items()}
                            mdata = {k:v for k,v in mdata.items() if isinstance(v, (int, float)) or _is_str(v) and not v.startswith('SHA256')}
                            #print('canonicalized version', mdata)
                            return mdata
                        items_i = get_items(mdatas[i])
                        items_j = get_items(mdatas[j])
                        print('\n\n\n')
                        #print('KEYS', items_i.keys())
                        assert items_i.keys() == items_j.keys()
                        print('------------differing items {}----------', which)
                        for k in sorted(items_i.keys()):
                            if items_i[k] != items_j[k]:
                                print(k, items_i[k], items_j[k])
                                if k == 'assemble_denovo.refine_2x_and_plot.assembly_length_unambiguous':
                                    pass
                        #print('\n'.join(sorted(map(str, items_i ^ items_j))))
                        print('--------end differing items {}-----------', which)
                        print('\n\n\n')
                    print('********* i={} j={}\n'.format(i,j))
                    show_diff('inputs')
                    show_diff('outputs')
                    print('***************************************')


#########################################################################################################################




def try_workflow(metadatajson):
    z = _json_loadf(metadatajson)
    leaf_jpaths = util.misc.json_gather_leaf_jpaths(z)
    str_leaves = list(filter(_is_str, util.misc.map_vals(leaf_jpaths)))
    for L in str_leaves:
        print('LEAF:', L)

    git_annex_tool = tools.git_annex.GitAnnexTool()
    imported = git_annex_tool.import_urls(str_leaves, ignore_non_urls=True)
    for url, filestat in imported.items():
        if filestat:
            print(url, str(filestat))

def parser_try_workflow(parser=argparse.ArgumentParser()):
    parser.add_argument('metadatajson', help='workflow metadata')
    util.cmd.attach_main(parser, try_workflow, split_args=True)
    return parser

#__commands__.append(('try_workflow', parser_try_workflow))


#########################################################################################################################

def try_workflow(metadatajson):
    z = _json_loadf(metadatajson)
    leaf_jpaths = util.misc.json_gather_leaf_jpaths(z)
    str_leaves = list(filter(_is_str, util.misc.map_vals(leaf_jpaths)))
    for L in str_leaves:
        print('LEAF:', L)

    git_annex_tool = tools.git_annex.GitAnnexTool()
    imported = git_annex_tool.import_urls(str_leaves, ignore_non_urls=True)
    for url, filestat in imported.items():
        if filestat:
            print(url, str(filestat))

def parser_try_workflow(parser=argparse.ArgumentParser()):
    parser.add_argument('metadatajson', help='workflow metadata')
    util.cmd.attach_main(parser, try_workflow, split_args=True)
    return parser

#__commands__.append(('try_workflow', parser_try_workflow))

#########################################################################################################################

def git_annex_get(fname):
    """Ensure given file is present in local annex"""
    tools.git_annex.GitAnnexTool().get(fname)
    util.misc.chk(os.path.isfile(fname))

def parser_git_annex_get(parser=argparse.ArgumentParser()):
    parser.add_argument('fname', help='file to get')
    util.cmd.attach_main(parser, git_annex_get, split_args=True)
    return parser

__commands__.append(('git_annex_get', parser_git_annex_get))

#########################################################################################################################


# * Epilog

def full_parser():
    return util.cmd.make_parser(__commands__, __doc__)

if __name__ == '__main__':
    #print(_pretty_print_json(_parse_cromwell_output_str(util.file.slurp_file('/dev/shm/cromwell_out/wdl_output.txt'))))
    #print(get_docker_hash('quay.io/broadinstitute/viral-ngs'))
    #record_run_results(0,0)
    if False:
        #_copy_to_gs(git_file_path='A4.scaffolding_chosen_ref.fasta', gs_prefix='gs://sabeti-ilya-cromwell')
        _json_to_org(_dx_describe('analysis-FK7BQ580761VgPb22550gfJv'), org_file='tmp/t.org')
        #_resolve_links_in_dx_analysis(dx_analysis_id='analysis-FK7BQ580761VgPb22550gfJv', analysis_dir='exper/dxan4')
        sys.exit(0)

    if False:
        run_dx_locally(workflow_name='assemble_denovo', analysis_dxid='analysis-FJfqjg005Z3Vp5Q68jxzx5q1',
                       docker_img='quay.io/broadinstitute/viral-ngs')
    #print('\n'.join(map(str, _flatten(_json_loadf('/data/ilya-work/pipelines/an-analysis-180914-200549-b9a24c15-bb50-409e-9fa9-9996e5c5ff37-assemble_denovo/metadata.json')))))
    if False:
        compare_analysis_pairs(analysis_dirs=[os.path.join('pipelines', d) for d in os.listdir('pipelines') \
                                              if os.path.isfile(os.path.join('pipelines', d, 'metadata.json'))],
                               filter_A={'inputs.assemble_denovo.assemble.assembler': 'trinity'},
                               filter_B={'inputs.assemble_denovo.assemble.assembler': 'spades'}, metrics=()
        )
    util.cmd.main_argparse(__commands__, __doc__)
