# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Test create publication target ssh web server action

"""

import os
from os import chmod
import stat
import re
from os.path import join as opj, exists

from ..dataset import Dataset
from datalad.api import publish, install, create_sibling
from datalad.utils import chpwd
from datalad.support.gitrepo import GitRepo
from datalad.support.annexrepo import AnnexRepo
from datalad.support.network import urlquote
from nose.tools import eq_, assert_false
from datalad.tests.utils import with_tempfile, assert_in, \
    with_testrepos
from datalad.tests.utils import ok_file_has_content
from datalad.tests.utils import ok_exists
from datalad.tests.utils import ok_clean_git
from datalad.tests.utils import ok_endswith
from datalad.tests.utils import assert_not_in
from datalad.tests.utils import assert_raises
from datalad.tests.utils import skip_ssh
from datalad.tests.utils import assert_dict_equal
from datalad.tests.utils import assert_set_equal
from datalad.tests.utils import assert_not_equal
from datalad.tests.utils import assert_no_errors_logged
from datalad.tests.utils import get_mtimes_and_digests
from datalad.tests.utils import swallow_logs
from datalad.tests.utils import ok_
from datalad.support.exceptions import CommandError
from datalad.support.exceptions import InsufficientArgumentsError

from datalad.utils import on_windows
from datalad.utils import _path_

import logging


def _test_correct_publish(target_path, rootds=False, flat=True):

    paths = [_path_(".git/hooks/post-update")]     # hooks enabled in all datasets
    not_paths = []  # _path_(".git/datalad/metadata")]  # metadata only on publish
                    # ATM we run post-update hook also upon create since it might
                    # be a reconfiguration (TODO: I guess could be conditioned)

    # web-interface html pushed to dataset root
    web_paths = ['index.html', _path_(".git/datalad/web")]
    if rootds:
        paths += web_paths
    # and not to subdatasets
    elif not flat:
        not_paths += web_paths

    for path in paths:
        ok_exists(opj(target_path, path))

    for path in not_paths:
        assert_false(exists(opj(target_path, path)))

    # correct ls_json command in hook content (path wrapped in quotes)
    ok_file_has_content(_path_(target_path, '.git/hooks/post-update'),
                        '.*datalad ls -a --json file \'%s\'.*' % target_path,
                        re_=True,
                        flags=re.DOTALL)


# shortcut
# but we can rely on it ATM only if "server" (i.e. localhost) has
# recent enough git since then we expect an error msg to be spit out
from datalad.support.external_versions import external_versions
# But with custom GIT_PATH pointing to non-bundled annex, which would not be
# used on remote, so we will compare against system-git
assert_create_sshwebserver = (
    assert_no_errors_logged(create_sibling)
    if external_versions['cmd:system-git'] >= '2.4'
    else create_sibling
)


@with_tempfile(mkdir=True)
def test_invalid_call(path):
    # needs a SSH URL
    assert_raises(InsufficientArgumentsError, create_sibling, '')
    assert_raises(ValueError, create_sibling, 'http://ignore.me')
    # needs an actual dataset
    assert_raises(
        ValueError,
        create_sibling, 'localhost:/tmp/somewhere', dataset='/nothere')
    # pre-configure a bogus remote
    ds = Dataset(path).create()
    ds.repo.add_remote('bogus', 'http://bogus.url.com')
    # fails to reconfigure by default with generated
    assert_raises(ValueError, ds.create_sibling, 'bogus:/tmp/somewhere')
    # and also when given an existing name
    assert_raises(
        ValueError,
        ds.create_sibling, 'localhost:/tmp/somewhere', name='bogus')


@skip_ssh
@with_testrepos('.*basic.*', flavors=['local'])
@with_tempfile(mkdir=True)
@with_tempfile(mkdir=True)
def test_target_ssh_simple(origin, src_path, target_rootpath):

    # prepare src
    source = install(src_path, source=origin)

    target_path = opj(target_rootpath, "basic")
    with swallow_logs(new_level=logging.ERROR) as cml:
        create_sibling(
            dataset=source,
            name="local_target",
            sshurl="ssh://localhost",
            target_dir=target_path,
            ui=True)
        assert_not_in('enableremote local_target failed', cml.out)

    GitRepo(target_path, create=False)  # raises if not a git repo
    assert_in("local_target", source.repo.get_remotes())
    # Both must be annex or git repositories
    src_is_annex = AnnexRepo.is_valid_repo(src_path)
    eq_(src_is_annex, AnnexRepo.is_valid_repo(target_path))
    # And target one should be known to have a known UUID within the source if annex
    if src_is_annex:
        annex = AnnexRepo(src_path)
        local_target_cfg = annex.repo.remotes["local_target"].config_reader.get
        # basic config in place
        eq_(local_target_cfg('annex-ignore'), 'false')
        ok_(local_target_cfg('annex-uuid'))

    # do it again without force, but use a different name to avoid initial checks
    # for existing remotes:
    with assert_raises(RuntimeError) as cm:
        assert_create_sshwebserver(
            dataset=source,
            name="local_target_alt",
            sshurl="ssh://localhost",
            target_dir=target_path)
    eq_("Target directory %s already exists." % target_path,
        str(cm.exception))
    if src_is_annex:
        target_description = AnnexRepo(target_path, create=False).get_description()
        assert_not_equal(target_description, None)
        assert_not_equal(target_description, target_path)
        ok_endswith(target_description, target_path)
    # now, with force and correct url, which is also used to determine
    # target_dir
    # Note: on windows absolute path is not url conform. But this way it's easy
    # to test, that ssh path is correctly used.
    if not on_windows:
        # add random file under target_path, to explicitly test existing=replace
        open(opj(target_path, 'random'), 'w').write('123')

        assert_create_sshwebserver(
            dataset=source,
            name="local_target",
            sshurl="ssh://localhost" + target_path,
            publish_by_default='master',
            existing='replace')
        eq_("ssh://localhost" + urlquote(target_path),
            source.repo.get_remote_url("local_target"))
        ok_(source.repo.get_remote_url("local_target", push=True) is None)

        # ensure target tree actually replaced by source
        assert_false(exists(opj(target_path, 'random')))

        if src_is_annex:
            annex = AnnexRepo(src_path)
            local_target_cfg = annex.repo.remotes["local_target"].config_reader.get
            eq_(local_target_cfg('annex-ignore'), 'false')
            eq_(local_target_cfg('annex-uuid').count('-'), 4)  # valid uuid
            # should be added too, even if URL matches prior state
            eq_(local_target_cfg('push'), 'master')

        # again, by explicitly passing urls. Since we are on localhost, the
        # local path should work:
        cpkwargs = dict(
            dataset=source,
            name="local_target",
            sshurl="ssh://localhost",
            target_dir=target_path,
            target_url=target_path,
            target_pushurl="ssh://localhost" + target_path,
            ui=True,
        )
        assert_create_sshwebserver(existing='replace', **cpkwargs)
        if src_is_annex:
            target_description = AnnexRepo(target_path,
                                           create=False).get_description()
            eq_(target_description, target_path)

        eq_(target_path,
            source.repo.get_remote_url("local_target"))
        eq_("ssh://localhost" + target_path,
            source.repo.get_remote_url("local_target", push=True))

        _test_correct_publish(target_path)

        # now, push should work:
        publish(dataset=source, to="local_target")

        # and we should be able to 'reconfigure'
        def process_digests_mtimes(digests, mtimes):
            # it should have triggered a hook, which would have created log and metadata files
            check_metadata = False
            for part in 'logs', 'metadata':
                metafiles = [k for k in digests if k.startswith(_path_('.git/datalad/%s/' % part))]
                # This is in effect ONLY if we have "compatible" datalad installed on remote
                # end. ATM we don't have easy way to guarantee that AFAIK (yoh),
                # so let's not check/enforce (TODO)
                # assert(len(metafiles) >= 1)  # we might have 2 logs if timestamps do not collide ;)
                # Let's actually do it to some degree
                if part == 'logs':
                    # always should have those:
                    assert (len(metafiles) >= 1)
                    with open(opj(target_path, metafiles[0])) as f:
                        if 'no datalad found' not in f.read():
                            check_metadata = True
                if part == 'metadata':
                    eq_(len(metafiles), bool(check_metadata))
                for f in metafiles:
                    digests.pop(f)
                    mtimes.pop(f)
            # and just pop some leftovers from annex
            for f in list(digests):
                if f.startswith('.git/annex/mergedrefs'):
                    digests.pop(f)
                    mtimes.pop(f)

        orig_digests, orig_mtimes = get_mtimes_and_digests(target_path)
        process_digests_mtimes(orig_digests, orig_mtimes)

        import time
        time.sleep(0.1)  # just so that mtimes change
        assert_create_sshwebserver(existing='reconfigure', **cpkwargs)
        digests, mtimes = get_mtimes_and_digests(target_path)
        process_digests_mtimes(digests, mtimes)

        assert_dict_equal(orig_digests, digests)  # nothing should change in terms of content

        # but some files should have been modified
        modified_files = {k for k in mtimes if orig_mtimes.get(k, 0) != mtimes.get(k, 0)}
        # collect which files were expected to be modified without incurring any changes
        ok_modified_files = {
            _path_('.git/hooks/post-update'), 'index.html',
            # files which hook would manage to generate
            _path_('.git/info/refs'), '.git/objects/info/packs'
        }
        # on elderly git we don't change receive setting
        ok_modified_files.add(_path_('.git/config'))
        ok_modified_files.update({f for f in digests if f.startswith(_path_('.git/datalad/web'))})
        assert_set_equal(modified_files, ok_modified_files)


@skip_ssh
@with_testrepos('submodule_annex', flavors=['local'])
@with_tempfile(mkdir=True)
@with_tempfile
def test_target_ssh_recursive(origin, src_path, target_path):

    # prepare src
    source = install(src_path, source=origin, recursive=True)[0]

    sub1 = Dataset(opj(src_path, "subm 1"))
    sub2 = Dataset(opj(src_path, "subm 2"))

    for flat in False, True:
        target_path_ = target_dir_tpl = target_path + "-" + str(flat)

        if flat:
            target_dir_tpl += "/prefix%RELNAME"
            sep = '-'
        else:
            sep = os.path.sep

        remote_name = 'remote-' + str(flat)
        with chpwd(source.path):
            assert_create_sshwebserver(
                name=remote_name,
                sshurl="ssh://localhost" + target_path_,
                target_dir=target_dir_tpl,
                recursive=True,
                ui=True)

        # raise if git repos were not created
        for suffix in [sep + 'subm 1', sep + 'subm 2', '']:
            target_dir = opj(target_path_, 'prefix' if flat else "").rstrip(os.path.sep) + suffix
            # raise if git repos were not created
            GitRepo(target_dir, create=False)

            _test_correct_publish(target_dir, rootds=not suffix, flat=flat)

        for repo in [source.repo, sub1.repo, sub2.repo]:
            assert_not_in("local_target", repo.get_remotes())

        # now, push should work:
        publish(dataset=source, to=remote_name)


@skip_ssh
@with_testrepos('submodule_annex', flavors=['local'])
@with_tempfile(mkdir=True)
@with_tempfile
def test_target_ssh_since(origin, src_path, target_path):
    # prepare src
    source = install(src_path, source=origin, recursive=True)[0]
    eq_(len(source.get_subdatasets()), 2)
    # get a new subdataset and make sure it is committed in the super
    source.create('brandnew')
    eq_(len(source.get_subdatasets()), 3)
    ok_clean_git(source.path)

    # and now we create a sibling for the new subdataset only
    assert_create_sshwebserver(
        name='dominique_carrera',
        dataset=source,
        sshurl="ssh://localhost" + target_path,
        recursive=True,
        since='HEAD~1')
    # there is one thing in the target directory only, and that is the
    # remote repo of the newly added subdataset

    target = Dataset(target_path)
    ok_(not target.is_installed())  # since we didn't create it due to since
    eq_(['brandnew'], os.listdir(target_path))

    # now test functionality if we add a subdataset with a subdataset
    brandnew2 = source.create('brandnew2')
    brandnewsub = brandnew2.create('sub')
    brandnewsubsub = brandnewsub.create('sub')
    # and now we create a sibling for the new subdataset only
    assert_create_sshwebserver(
        name='dominique_carrera',
        dataset=source,
        sshurl="ssh://localhost" + target_path,
        recursive=True,
        existing='skip')
    # verify that it created the sub and sub/sub
    ok_(Dataset(_path_(target_path, 'brandnew2/sub')).is_installed())
    ok_(Dataset(_path_(target_path, 'brandnew2/sub/sub')).is_installed())


@skip_ssh
@with_tempfile(mkdir=True)
@with_tempfile(mkdir=True)
def test_failon_no_permissions(src_path, target_path):
    ds = Dataset(src_path).create()
    # remove user write permissions from target path
    chmod(target_path, stat.S_IREAD | stat.S_IEXEC)
    assert_raises(
        CommandError,
        ds.create_sibling,
        name='noperm',
        sshurl="ssh://localhost" + opj(target_path, 'ds'))
    # restore permissions
    chmod(target_path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    assert_create_sshwebserver(
        name='goodperm',
        dataset=ds,
        sshurl="ssh://localhost" + opj(target_path, 'ds'))
