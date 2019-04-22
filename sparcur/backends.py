import os
import atexit
import subprocess
from datetime import datetime
import requests
from pexpect import pxssh
from sparcur import exceptions as exc
from sparcur.core import log
from sparcur.paths import PathMeta, RemotePath, CachePath, LocalPath, Path
from sparcur.config import local_storage_prefix

from sparcur.blackfynn_api import BFLocal  # FIXME there should be a better way ...
from blackfynn import Collection, DataPackage, Organization, File
from blackfynn import Dataset
from blackfynn.models import BaseNode

from ast import literal_eval


class ReflectiveCachePath(CachePath):
    """ Oh, it's me. """

    @property
    def id(self):
        return self.resolve().as_posix()


class RemoteFactory:
    """ Assumes that Path is a parent. """
    def ___new__(cls, *args, **kwargs):
        # NOTE this should NOT be tagged as a classmethod
        # it is accessed at cls time already and tagging it
        # will cause it to bind to the original insource parent
        return super().__new__(cls)#, *args, **kwargs)

    def __new__(cls, local_class, cache_class, **kwargs):
        # TODO use this call to set the remote of local and cache??
        kwargs['_local_class'] = local_class
        kwargs['_cache_class'] = cache_class
        newcls = cls._bindKwargs(**kwargs)
        newcls.__new__ = cls.___new__
        # FIXME klobbering and how to handle multiple?
        local_class._remote_class = newcls
        local_class._cache_class = cache_class
        cache_class._remote_class = newcls
        return newcls

    @classmethod
    def _bindKwargs(cls, **kwargs):
        new_name = cls.__name__.replace('Factory','')
        classTypeInstance = type(new_name,
                                 (cls,),
                                 kwargs)
        return classTypeInstance


class StatResult:
    stat_format = f'\"%n  %o  %s  %w  %W  %x  %X  %y  %Y  %z  %Z  %g  %u  %f\"'

    #stat_format = f'\"\'%n\' %o %s \'%w\' %W \'%x\' %X \'%y\' %Y \'%z\' %Z %g %u %f\"'

    def __init__(self, out):
        out = out.decode()
        #name, rest = out.rsplit("'", 1)
        #self.name = name.strip("'")
        #print(out)
        wat = out.split('  ')
        #print(wat)
        #print(len(wat))
        name, hint, size, hb, birth, ha, access, hm, modified, hc, changed, gid, uid, raw_mode = wat

        self.name = name

        def ns(hr):
            date, time, zone = hr.split(' ')
            time, ns = time.split('.')
            return '.' + ns

        self.st_blksize = int(hint)
        self.st_size = int(size)
        #self.st_birthtime
        self.st_atime = float(access + ns(ha)) 
        self.st_mtime = float(modified + ns(hm))
        self.st_ctime = float(changed + ns(hc))
        self.st_gid = int(gid)
        self.st_uid = int(uid)
        self.st_mode = int.from_bytes(bytes.fromhex(raw_mode), 'big')

        if hb != '-' and birth != '0':
            self.st_birthtime = float(birth + ns(hb))


class SshRemoteFactory(RemoteFactory, RemotePath):
    """ Testing. To be used with ssh-agent.
        StuFiS The stupid file sync. """

    cypher_command = 'sha256sum'
    encoding = 'utf-8'

    def __new__(cls, local_class, cache_class, host):
        session = pxssh.pxssh(options=dict(IdentityAgent=os.environ.get('SSH_AUTH_SOCK')))
        session.login(host, ssh_config=Path('~/.ssh/config').expanduser().as_posix())
        cls._rows = 200
        cls._cols = 200
        session.setwinsize(cls._rows, cls._cols)  # prevent linewraps of long commands
        session.prompt()
        atexit.register(lambda:(session.sendeof(), session.close()))
        return super().__new__(cls, local_class, cache_class, host=host, session=session)

    def refresh(self):
        # TODO probably not the best idea ...
        raise NotImplementedError('This baby goes to the network every single time!')

    @property
    def id(self):
        # this allows a remapping once
        # otherwise we face chicken and egg problem
        # or rather, in this case, the remote system
        # really doesn't know how we've mapped something
        # locally, I guess it could, but for the implementaiton
        # we don't track that right now
        return self.cache.id

    @property
    def data(self):
        cmd = ['scp', f'{self.host}:{self.cache.id}', '/dev/stdout']
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        while True:
            data = p.stdout.read(4096)  # TODO hinting
            if not data:
                break

            yield data

        p.communicate()

    # reuse meta from local
    # def meta (make it easier to search for this)
    meta = LocalPath.meta  # magic

    def _ssh(self, remote_cmd):
        #print(remote_cmd)
        if len(remote_cmd) > self._cols:
            raise exc.CommandTooLongError
        n_bytes = self.session.sendline(remote_cmd)
        self.session.prompt()
        raw = self.session.before
        out = raw[n_bytes + 1:].strip()  # strip once here since we always will
        #print(raw)
        #print(out)
        return out

    def checksum(self):
        remote_cmd = (f'{self.cypher_command} {self.cache.id} | '
                      'awk \'{ print $1 }\';')

        return bytes.fromhex(self._ssh(remote_cmd).decode(self.encoding))

    def stat(self):
        remote_cmd = f'stat "{self.cache.id}" -c {StatResult.stat_format}'
        out = self._ssh(remote_cmd)
        return StatResult(out)

    @property
    def parent(self):
        # because the identifiers are paths if we move
        # file.ext to another folder, we treat it as if it were another file
        # at least for this SshRemote path, if we move a file on our end
        # the we had best update our cache
        # if someone else moves the file on the remote, well, then
        # that file simply vanishes since we weren't notified about it
        # if there is a remote transaction log we can replay if there isn't
        # we have to assume the file was deleted or check all the names and
        # hashes of new files to see if it has moved (and not been changed)
        # a move and change without a sync will be bad for us

        # If you have an unanchored path then resolve()
        # always operates under the assumption that the
        # current working directory which I think is incorrect
        # as soon as you start passing unresolved paths around
        # the remote system doesn't know what context you are in
        # so we need to fail loudly
        # basically force people to manually resolve their paths
        return self.__class__(self.cache.parent)

    def is_dir(self):
        remote_cmd = f'stat -c %F {self.cache.id}'
        out = self._ssh(remote_cmd)
        return out == b'directory'

    @property
    def children(self):
        # this is amusingly bad, also children_recursive ... drop the maxdepth
        #("find ~/files/blackfynn_local/SPARC\ Consortium -maxdepth 1 "
        #"-exec stat -c \"'%n' %o %s %W %X %Y %Z %g %u %f\" {} \;")
        # chechsums when listing children? maybe ...
        #\"'%n' %o %s %W %X %Y %Z %g %u %f\"
        if self.is_dir():
            # no children if it is a file sadly
            remote_cmd = (f"cd {self.cache.id};"
                          f"stat -c {StatResult.stat_format} {{.,}}*;"
                          "echo '----';"
                          f"{self.cypher_command} {{.,}}*;"  # FIXME fails on directories destroying alignment
                          'cd "${OLDPWD}"')

            out = self._ssh(remote_cmd)
            stats, checks = out.split(b'\r\n----\r\n')
            #print(stats)
            stats = {sr.name:sr for s in stats.split(b'\r\n')
                     for sr in (StatResult(s),)}
            checks = {fn:bytes.fromhex(cs) for l in checks.split(b'\r\n')
                      if not b'Is a directory' in l
                      for cs, fn in (l.decode(self.encoding).split('  ', 1),)}

            return stats, checks  # TODO


class BlackfynnRemoteFactory(RemoteFactory, RemotePath):
    # FIXME helper index should try to cooperate with the packages index?

    def __new__(cls, anchor_or_bfl, local_class, cache_class):
        if isinstance(anchor_or_bfl, BFLocal):
            blackfynn_local_instance = BFLocal
        else:
            try:
                blackfynn_local_instance = BFLocal(anchor_or_bfl.id)
            except requests.exceptions.ConnectionError:
                blackfynn_local_instance = 'Could not connect to blackfynn'
                log.critical(blackfynn_local_instance)

        self = super().__new__(cls, local_class, cache_class, bfl=blackfynn_local_instance)
        self.root = self.bfl.organization.id
        return self

    def __init__(self, id_bfo_or_bfr, cache=None, helper_index=None):

        self.errors = []
        if helper_index is not None:
            self.helper_index.update(helper_index)

        # set _cache to avoid the id equality check since this is in __init__
        if cache is not None:
            self._cache = cache

        if isinstance(id_bfo_or_bfr, self.__class__):
            bfobject = id_bfo_or_bfr.bfobject
        elif isinstance(id_bfo_or_bfr, BaseNode):
            bfobject = id_bfo_or_bfr
        elif id_bfo_or_bfr.startswith('N:organization:'):
            if hasattr(self.__class__, '_organization'):
                raise TypeError('You already have a perfectly good organization')

            elif id_bfo_or_bfr == self.root:
                if self.cache is None:
                    breakpoint()
                    raise ValueError('where is your cache m8 you are root!')

                self.__class__._organization = self
                self.bfobject = self.bfl.organization
                return

        else:
            #raise ValueError(f'why are you doing this {id_bfo_or_bfr}')  # because refresh
            bfobject = self.bfl.get(id_bfo_or_bfr)

        if bfobject is None:
            raise TypeError('bfobject cannot be None!')

        elif isinstance(bfobject, str):
            raise TypeError(f'bfobject cannot be str! {bfobject}')

        elif bfobject.id.startswith('N:organization:') and hasattr(self.__class__, '_organization'):
            raise TypeError('You already have a perfectly good organization')

        self.bfobject = bfobject

    def is_anchor(self):
        return self.anchor == self

    @property
    def anchor(self):
        """ NOTE: this is a slight depature from the semantics in pathlib
            because this returns the path representation NOT the string """
        return self.organization

    @property
    def organization(self):
        if not hasattr(self.__class__, '_organization'):
            self.__class__._organization = self.__class__(self.bfl.organization)

        return self._organization

    @property
    def name(self):
        name = self.bfobject.name
        if '/' in name:
            bads = ','.join(f'{i}' for i, c in enumerate(name) if c == '/')
            self.errors.append(f'slashes {bads}')
            log.critical(f'GO AWAY {self}')
            name = name.replace('/', '_')
            self.bfobject.name = name  # AND DON'T BOTHER US AGAIN

        if ([t for t in (DataPackage, File) if isinstance(self.bfobject, t)] and
            self.bfobject.type != 'Unknown'):
            # NOTE we have to use blackfynns extensions until we retrieve the files
            name += '.' + self.bfobject.type.lower()  # FIXME ... can we match s3key?

        return name

    @property
    def id(self):
        if isinstance(self.bfobject, File):
            return self.bfobject.pkg_id
        else:
            return self.bfobject.id

    @property
    def size(self):
        if isinstance(self.bfobject, File):
            return self.bfobject.size

    @property
    def created(self):
        if not isinstance(self.bfobject, Organization):
            return self.bfobject.created_at

    @property
    def updated(self):
        if not isinstance(self.bfobject, Organization):
            return self.bfobject.updated_at

    @property
    def file_id(self):
        if isinstance(self.bfobject, File):
            return self.bfobject.id

    @property
    def old_id(self):
        return None

    def is_dir(self):
        bfo = self.bfobject
        return not isinstance(bfo, File) and not isinstance(bfo, DataPackage)

    def is_file(self):
        bfo = self.bfobject
        return isinstance(bfo, File) or isinstance(bfo, DataPackage) and not list(self.children)

    @property
    def checksum(self):
        if hasattr(self.bfobject, 'checksum'):
            return self.bfobject.checksum
        # log this downstream, since downstream also knows the file affected
        #elif isinstance(self.bfobject, File):
            #log.warning(f'No checksum for {self}')

    @property
    def owner_id(self):
        if not isinstance(self.bfobject, Organization):
            # This seems like an oversight ...
            return self.bfobject.owner_id

    @property
    def parent(self):
        if isinstance(self.bfobject, Organization):
            return None

        elif isinstance(self.bfobject, Dataset):
            parent = self.bfobject._api._context

        else:
            parent = self.bfobject.parent
            if parent is None:
                parent = self.bfobject.dataset

        if False and isinstance(parent, str):
            if parent in self.helper_index:
                return self.helper_index[parent]
            else:
                breakpoint()
                raise TypeError('grrrrrrrrrrrrrrr')

        if parent:
            return self.__class__(parent, self.cache.parent)

    @property
    def parents(self):
        parent = self.parent
        while parent:
            yield parent
            parent = parent.parent

    @property
    def children(self):
        if isinstance(self.bfobject, File):
            return
        elif isinstance(self.bfobject, DataPackage):
            return  # we conflate data packages and files
        elif isinstance(self.bfobject, Organization):
            for dataset in self.bfobject.datasets:
                child = self.__class__(dataset)
                self.cache / child  # construction will cause registration without needing to assign
                #breakpoint()
                assert child.cache
                yield child
        else:
            for bfobject in self.bfobject:
                child = self.__class__(bfobject)
                self.cache / child  # construction will cause registration without needing to assign
                assert child.cache
                yield child

    @property
    def rchildren(self):
        if isinstance(self.bfobject, File):
            return
        elif isinstance(self.bfobject, DataPackage):
            return  # should we return files inside packages? are they 1:1?
        elif any(isinstance(self.bfobject, t) for t in (Organization, Collection)):
            for child in self.children:
                yield child
                yield from child.rchildren
        elif isinstance(self.bfobject, Dataset):
            for bfobject in self.bfobject.packages:
                child = self.__class__(bfobject)
                self.cache / child  # construction will cause registration without needing to assign
                assert child.cache is not None
                yield child
        else:
            raise exc.UnhandledTypeError  # TODO

    def isinstance_bf(self, *types):
        return [t for t in types if isinstance(self.bfobject, t)]

    def refresh(self, update_cache=False):
        old_meta = self.meta
        super().refresh()
        if self.isinstance_bf(File):
            file_id = self.meta.file_id
            if file_id:
                package = self.bfl.get(self.id)
                for i, file in enumerate(package.files):
                    if file.id == file_id:
                        self.bfobject = file

                if i:
                    log.critical(f'MORE THAN ONE FILE IN PACKAGE {package.id}')

            else:
                log.warning('FIXME File refreshing not implemented')
                return

        elif self.isinstance_bf(DataPackage):
            package = self.bfl.get(self.id)
            self.bfobject = package  # for the time being
            files = package.files  # this makes me sad :/ all I needed was the file id and the size >_<
            for i, file in enumerate(files):
                self.bfobject = file

            if i:
                log.critical(f'MORE THAN ONE FILE IN PACKAGE {package.id}')

        elif self.isinstance_bf(Collection):
            self.bfobject = self.bfl.get(self.id)

        # FIXME all of this needs to go in cache._meta_setter which is where the only
        # meaningful difference should be detected and managed
        # alternately it may need to go inbetween or before, becuase
        # after the changes the old cache might not exist anymore
        new_meta = self.meta

        assert new_meta is not old_meta

        object_type = self.bfobject.__class__.__name__
        actions = []
        for k, old_v in old_meta.items():
            new_v = getattr(new_meta, k)
            if old_v is None:
                action = 'new', object_type, k, new_v
                actions.append(action)
                continue  # don't compare missing fields

            if new_v != old_v:
                action = 'changed', object_type, k, old_v, new_v
                actions.append(action)

        # in the fancy version of this that runs as a total proveance store
        # well, that is on actually on the other side ... because this is the
        # code that pulls data, not the code that waits for things that want to push
        # but, this is sort of where one would want to log all the transactions
        log.debug(f'things needing action: {self.name: <60} {actions}')  # TODO  processing actions move change update delete
        if update_cache:
            c, d = new_meta.__reduce__()
            om = c(**d)
            log.debug(f'updating cache {om}')
            assert self.meta is new_meta
            self.cache.meta = new_meta

    @property
    def data(self):
        if isinstance(self.bfobject, DataPackage):
            files = list(self.bfobject.files)
            if len(files) > 1:
                raise BaseException('TODO too many files')

            file = files[0]
        elif isinstance(self.bfobject, File):
            file = self.bfobject
        else:
            return

        resp = requests.get(file.url, stream=True)
        log.debug(f'reading from to {file.url}')
        for chunk in resp.iter_content(chunk_size=4096):  # FIXME align chunksizes between local and remote
            if chunk:
                yield chunk

    @property
    def meta(self):
        # since BFR is a remote it is OK to memoize the meta
        # because we will have to go to the net to get the new version
        # which probably will be implemented as just creating a whole
        # new one of these and switching it out
        if not hasattr(self, '_meta'):
            if self.errors:
                errors = tuple(self.errors)
            else:
                errors = tuple()

            self._meta = PathMeta(size=self.size,
                                  created=self.created,
                                  updated=self.updated,
                                  checksum=self.checksum,
                                  id=self.id,
                                  file_id=self.file_id,
                                  old_id=None,
                                  gid=None,  # needed to determine local writability
                                  user_id=self.owner_id,
                                  mode=None,
                                  errors=errors)

        return self._meta

    def __eq__(self, other):
        return self.bfobject == other.bfobject

    def __repr__(self):
        return f'{self.__class__.__name__}({self.id})'



def main():
    from IPython import embed
    embed()


if __name__ == '__main__':
    main()
