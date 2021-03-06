"""
Internal mechanisms for experiment logging.
"""

from typing import Optional, List, Set, Dict, Any
from abc import ABC, abstractmethod
import sys
import atexit
import warnings
import os
import subprocess
import tempfile
import platform
import inspect
import shutil
import tarfile
import time
import datetime
import uuid
import pickle
import json
import warnings

import xxhash
import psutil
import dill
import git


__all__ = [
    'Artifact',
    'get_script_path',
    'ensure_dir_exists',
    'eval_checksum',
    'Experiment',
    'experiment',
]


start_timestamp = time.time()
start_datetime = datetime.datetime.now()


def get_script_path() -> str:
    """
    Returns the path to the currently running script.
    """

    path = os.path.abspath(sys.argv[0])
    assert os.path.exists(path)
    return path


def ensure_dir_exists(path: str) -> str:
    """
    Ensure that the the given path exists and is a directory. Returns the absolute path.
    """

    path = os.path.abspath(os.path.realpath(path))

    if not os.path.exists(path):
        os.makedirs(path)

    return os.path.abspath(path)


def eval_checksum(path: str,
                  state: Optional[xxhash.xxh3_64] = None,
                  digest: bool = True,
                  base_path: Optional[str] = None) \
        -> Optional[str]:
    """
    Evaluates the checksum of the contents of a given path (which can be either a file or a directory).
    """

    path = os.path.abspath(os.path.realpath(path))
    base_path = base_path or (path if os.path.isdir(path) else os.path.dirname(path))

    if state is None:
        state = xxhash.xxh3_64()

    state.update(os.path.relpath(path, base_path))

    if os.path.isfile(path):
        filesize = os.path.getsize(path)
        available_memory = psutil.virtual_memory().available
        if 10*filesize <= available_memory:
            with open(path, 'rb') as file:
                state.update(file.read())
        else:
            chunksize = max(available_memory//10, state.block_size)
            with open(path, 'rb') as file:
                while True:
                    chunk = file.read(chunksize)
                    if not chunk:
                        break
                    state.update(chunk)
    elif os.path.isdir(path):
        for child in sorted(os.listdir(path)):
            eval_checksum(os.path.join(path, child), state=state, digest=False, base_path=base_path)

    if digest:
        return state.hexdigest()


class Artifact(ABC):
    """
    Represents that an object to be saved should be saved in a special way (instead of defaulting to
    using `dill`).
    """

    @abstractmethod
    def write(self, path: str) -> None:
        """
        Write the object to the path `path`.
        """

    @abstractmethod
    def read(self, path: str) -> Any:
        """
        Read the object from the path `path`.
        """


# pylint: disable=too-few-public-methods
class Experiment:
    """
    Represents an experiment to be logged.
    """

    uuid: str
    """ The experiment's ID. """
    no_save: bool
    """ If `True`, then don't save the experiment (nor setup stdout/stderr indirection). """

    has_content: bool
    """ A member variable that tracks whether there is anything to log. """
    output_files: Set[str]
    """ A list of files that the running script has produced as output. """
    input_files: Set[str]
    """ A list of files that the running script has taken as input. """
    extra_keys: Dict[str, Any]
    """ A dictionary containing extra information to store as part of the experiment. """
    multiple: Dict[str, bool]
    """ A dictionary denoting, for each element of `extra_keys`, whether they represent a sequence
    of data (multiple data). """

    def __init__(self):
        self.uuid = str(uuid.uuid4())
        self.no_save = hasattr(sys, 'ps1')  # True if in interactive shell

        self.has_content = False
        self.output_files = set()
        self.input_files = set()
        self.extra_keys = {}
        self.multiple = {}

        tmpstdout_handle, self.stdout_file = tempfile.mkstemp()  # 'stdout.out'
        tmpstderr_handle, self.stderr_file = tempfile.mkstemp()  # 'stderr.out'
        os.close(tmpstdout_handle)
        os.close(tmpstderr_handle)

        self._setup_stdout_redirection(self.stdout_file, self.stderr_file)

    def add_output_file(self, path: str) -> None:
        """
        Add an output file to be logged (a copy of the file is saved).
        """

        self.has_content = True
        self.output_files.add(path)

    def add_input_file(self, path: str) -> None:
        """
        Add an input file to be logged (only its checksum is saved).
        """

        self.has_content = True
        self.input_files.add(path)

    def add_extra_key(self, key: str, value: Any, overwrite: bool = True) -> None:
        """
        Add some arbitrary information to be saved with the experiment.

        If the key specified by `key` already exists, behaviour depends on the value of `overwrite`:
        if `overwrite` is `True`, then a warning is thrown and the value is overwritten; if
        `overwrite` is `False`, then both (or however many) values are kept in a list.
        """

        self.has_content = True
        if key in self.extra_keys:
            if overwrite:
                warnings.warn("Overwriting key '%s' in experiment" % key)
                self.extra_keys[key] = value
                self.multiple[key] = False
            else:
                if self.multiple[key]:
                    self.extra_keys[key].append(value)
                else:
                    self.multiple[key] = True
                    self.extra_keys[key] = [self.extra_keys[key], value]
        else:
            self.extra_keys[key] = value
            self.multiple[key] = False

    def _setup_stdout_redirection(self, stdout_file: str, stderr_file: str) -> None:
        if not self.no_save:
            tee_stdout = subprocess.Popen(['tee', stdout_file], stdin=subprocess.PIPE, start_new_session=True)
            os.dup2(tee_stdout.stdin.fileno(), sys.stdout.fileno())
            tee_stderr = subprocess.Popen(['tee', stderr_file], stdin=subprocess.PIPE, start_new_session=True)
            os.dup2(tee_stderr.stdin.fileno(), sys.stderr.fileno())

    def _cleanup(self) -> None:
        if os.path.exists(self.stdout_file):
            os.remove(self.stdout_file)
        if os.path.exists(self.stderr_file):
            os.remove(self.stderr_file)

    def _get_root_path(self) -> str:
        rootpath = os.environ.get('LOGRUN_ROOT')
        if rootpath is not None:
            return rootpath

        if os.path.isfile('.logrun.json'):
            with open('.logrun.json') as file:
                data = json.load(file)
            rootpath = data.get('logrun-root')
            if rootpath is not None:
                return rootpath

        DEFAULT_NAME = 'logrun-logs.tmp'
        warnings.warn(f"No place to save the experiments was given -- defaulting to `{DEFAULT_NAME}`.")
        return DEFAULT_NAME

    def save_experiment(self) -> None:
        """
        Save the experiment to the experiment root path.
        """

        from logrun import __version__

        if not self.has_content or self.no_save:
            self._cleanup()
            return

        rootpath = self._get_root_path()

        print("[Saving experiment: %s]" % self.uuid)

        experiment_path = \
            ensure_dir_exists(os.path.join(rootpath, 'all_experiments', self.uuid))
        experiment_path_targz = experiment_path + '.tar.gz'
        experiment_by_outfile_path = \
            ensure_dir_exists(os.path.join(rootpath, 'experiments_by_output_file'))
        experiment_by_infile_path = \
            ensure_dir_exists(os.path.join(rootpath, 'experiments_by_input_file'))
        experiment_source_directory = ensure_dir_exists(os.path.join(experiment_path, 'source'))

        for module in sys.modules.values():
            if not module.__name__.startswith('src.'):
                continue

            try:
                source_file = inspect.getsourcefile(module)
                inspect.getsource(module)
            except (TypeError, OSError):
                # If we are here then this module doesn't have a corresponding source file
                # (or it's compiled).
                continue

            if os.path.getmtime(source_file) > start_timestamp:
                print("Warning: source file [%s] modified since start of program execution!")

            with open(os.path.join(experiment_source_directory,
                                   os.path.relpath(source_file).replace(os.sep, '%')), 'w') \
                    as file:
                file.write(inspect.getsource(module))
        source_file = sys.argv[0]
        if os.path.getmtime(source_file) > start_timestamp:
            print("Warning: script file [%s] modified since start of execution!" % source_file)
        with open(os.path.join(experiment_source_directory,
                               os.path.relpath(source_file).replace(os.sep, '%')), 'w') as file:
            with open(source_file) as scriptfile:
                file.write(scriptfile.read())

        with open(os.path.join(experiment_path, 'meta.pickle'), 'wb') as file:
            metadata = {
                'host':                 platform.node(),
                'user':                 os.getlogin(),
                'cwd':                  os.getcwd(),
                'argv':                 sys.argv,
                'logrun_version':       __version__,
                'start_execution_time': start_datetime,
                'end_execution_time':   datetime.datetime.now(),
                'environment':          dict(os.environ),
            }
            try:
                gitrepo = git.Repo(search_parent_directories=True)
                metadata['gitcommit'] = gitrepo.head.object.hexsha
            except git.exc.InvalidGitRepositoryError:
                pass
            pickle.dump(metadata, file)

        shutil.copyfile(self.stdout_file, os.path.join(experiment_path, 'stdout.out'))
        shutil.copyfile(self.stderr_file, os.path.join(experiment_path, 'stderr.out'))

        output_files_path = ensure_dir_exists(os.path.join(experiment_path, 'output_files'))
        for output_file in self.output_files:
            if not os.path.exists(output_file):
                print("Warning: output file does not exist: '%s'; skipping this one..."
                      % output_file)
                continue

            output_file_repr = output_file.replace(os.sep, '%')
            if os.path.isfile(output_file):
                shutil.copyfile(output_file, os.path.join(output_files_path, output_file_repr))
            else:
                shutil.copytree(output_file, os.path.join(output_files_path, output_file_repr))
            with open(os.path.join(experiment_by_outfile_path, output_file_repr), 'a') as file:
                print(self.uuid, file=file)
        with open(os.path.join(experiment_path, 'input_files.pickle'), 'wb') as file:
            pickle.dump({input_file: eval_checksum(input_file)
                         for input_file in self.input_files},
                        file)
        for input_file in self.input_files:
            input_file_repr = input_file.replace(os.sep, '%')
            with open(os.path.join(experiment_by_infile_path, input_file_repr), 'a') as file:
                print(self.uuid, file=file)

        extra_keys_path = ensure_dir_exists(os.path.join(experiment_path, 'extra_keys'))

        for base_key, base_value in self.extra_keys.items():
            if self.multiple[base_key]:
                keyvals = {base_key + (f'.%0{len(str(len(base_value)))}d') % k: base_value[k]
                           for k in range(len(base_value))}
            else:
                keyvals = {base_key: base_value}
            for key, value in keyvals.items():
                path = os.path.join(extra_keys_path, key)
                if isinstance(value, Artifact):
                    value.write(path)
                    with open(path + '.read', 'wb') as file:
                        dill.dump(value.read, file)
                else:
                    with open(path, 'wb') as file:
                        dill.dump(value, file)

        with tarfile.open(experiment_path_targz, 'w:gz') as tar_handle:
            for root, _, files in os.walk(experiment_path):
                for file in files:
                    tar_handle.add(os.path.join(root, file))
        shutil.rmtree(experiment_path)

        self._cleanup()


experiment = Experiment()
atexit.register(experiment.save_experiment)
