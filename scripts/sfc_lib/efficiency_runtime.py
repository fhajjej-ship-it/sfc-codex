"""Prepare a local Python runtime with empty site packages for reusable SFC checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile

from .common import Halt, common_dir
from .verification import checksum, digest

FOLDER = 'sfc-check-runtimes'
KEYS = {'version', 'launcher', 'executable', 'executable_sha256', 'stdlib',
        'base_prefix', 'native_libraries', 'location'}


def location_key(descriptor):
    return digest({k: v for k, v in descriptor.items() if k != 'location'})


def validate(descriptor):
    if (not isinstance(descriptor, dict) or set(descriptor) != KEYS or
            not isinstance(descriptor['version'], str) or
            not re.fullmatch(r'\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?', descriptor['version'])):
        raise Halt('Invalid Python check runtime descriptor')
    paths = ('launcher', 'executable', 'stdlib', 'base_prefix', 'location')
    if (any(not isinstance(descriptor[k], str) or not Path(descriptor[k]).is_absolute() or
            '\0' in descriptor[k] or '..' in Path(descriptor[k]).parts for k in paths) or
            not isinstance(descriptor['native_libraries'], dict) or not isinstance(descriptor['executable_sha256'], str) or
            not re.fullmatch(r'[0-9a-f]{64}', descriptor['executable_sha256'])):
        raise Halt('Invalid Python check runtime paths or executable hash')
    for path, sha in descriptor['native_libraries'].items():
        if (not isinstance(path, str) or not Path(path).is_absolute() or '\0' in path or
                '..' in Path(path).parts or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha)):
            raise Halt('Invalid Python shared library fingerprint')
    location = Path(descriptor['location'])
    if location.parent.name != FOLDER or location.name != location_key(descriptor):
        raise Halt('Invalid Python check runtime location')


def plan(root):
    """Detect a supported interpreter and propose its repository-common runtime."""
    library = sysconfig.get_path('stdlib')
    if os.name != 'posix' or not library:
        return None
    python = shutil.which('python3')
    launcher = Path(getattr(sys, '_base_executable', None) or sys.executable).absolute()
    try:
        executable = launcher.resolve(strict=True)
        if not python or Path(python).resolve(strict=True) != executable:
            return None
        stdlib = Path(library).resolve(strict=True)
        if not stdlib.is_dir():
            return None
        framework = sysconfig.get_config_var('PYTHONFRAMEWORK')
        libdir = sysconfig.get_config_var('LIBDIR')
        libname = sysconfig.get_config_var('LDLIBRARY')
        libraries = []
        if framework:
            libraries.append(Path(sys.base_prefix) / framework)
        elif sysconfig.get_config_var('PY_ENABLE_SHARED') and libdir and libname:
            libraries.append(Path(libdir) / libname)
        if sysconfig.get_config_var('PY_ENABLE_SHARED') and not libraries:
            return None
        descriptor = {'version': platform.python_version(), 'launcher': str(launcher),
                      'executable': str(executable), 'executable_sha256': checksum(executable),
                      'stdlib': str(stdlib), 'base_prefix': str(Path(sys.base_prefix).resolve()),
                      'native_libraries': {str(path): checksum(path) for path in libraries}}
        descriptor['location'] = str(common_dir(root) / FOLDER / location_key(descriptor))
        validate(descriptor)
        environment(Path(descriptor['location']))
        return descriptor
    except (OSError, RuntimeError, ValueError):
        return None


def host_matches(descriptor, budget):
    launcher = Path(descriptor['launcher'])
    if (str(launcher.resolve(strict=True)) != descriptor['executable'] or not os.access(launcher, os.X_OK) or
            checksum(launcher, budget) != descriptor['executable_sha256'] or
            not Path(descriptor['stdlib']).is_dir()):
        raise ValueError('Python interpreter or standard library location changed')
    for path, sha in descriptor['native_libraries'].items():
        if checksum(path, budget) != sha:
            raise ValueError('Python shared library changed')


def versioned_python(descriptor):
    return 'python' + '.'.join(descriptor['version'].split('.')[:2])


def structure(runtime, descriptor, budget):
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError('Python check runtime is unavailable or replaced')
    config = runtime / 'pyvenv.cfg'
    if config.is_symlink() or not config.is_file() or config.stat().st_size > 8192:
        raise ValueError('Python check runtime configuration is unavailable or replaced')
    config_digest = checksum(config, budget)
    lines = [line.split('=', 1) for line in config.read_text().splitlines() if line.strip()]
    values = {key.strip(): value.strip() for key, value in lines}
    home = Path(values.get('home', ''))
    expected_homes = {Path(descriptor[k]).parent.resolve() for k in ('launcher', 'executable')}
    if (len(values) != len(lines) or set(values) - {'home', 'include-system-site-packages', 'version',
                                                 'executable', 'command', 'prompt'} or
            values.get('include-system-site-packages') != 'false' or values.get('version') != descriptor['version'] or
            not home.is_absolute() or home.resolve() not in expected_homes or
            ('executable' in values and values['executable'] != descriptor['executable'])):
        raise ValueError('Python check runtime configuration changed')
    bin_dir, lib_dir = runtime / 'bin', runtime / 'lib'
    python_names = {'python', 'python3', versioned_python(descriptor)}
    if bin_dir.is_symlink() or {p.name for p in bin_dir.iterdir()} != python_names:
        raise ValueError('Python check runtime executable inventory changed')
    for name in python_names:
        path = bin_dir / name
        if not path.is_symlink() or str(path.resolve(strict=True)) != descriptor['executable']:
            raise ValueError('Python check runtime executable changed')
    py_dir = lib_dir / versioned_python(descriptor)
    site = py_dir / 'site-packages'
    include = runtime / 'include'
    lib64 = runtime / 'lib64'
    if (set(p.name for p in runtime.iterdir()) - {'bin', 'lib', 'lib64', 'include', 'pyvenv.cfg'} or
            lib_dir.is_symlink() or py_dir.is_symlink() or site.is_symlink() or
            {p.name for p in lib_dir.iterdir()} != {py_dir.name} or
            {p.name for p in py_dir.iterdir()} != {site.name} or next(site.iterdir(), None) is not None or
            (os.path.lexists(include) and (include.is_symlink() or not include.is_dir() or next(include.iterdir(), None))) or
            (os.path.lexists(lib64) and (not lib64.is_symlink() or lib64.resolve(strict=True) != lib_dir))):
        raise ValueError('Python check runtime contains unbound site or package files')
    return config_digest


def location(descriptor, root):
    validate(descriptor)
    runtime = Path(descriptor['location'])
    if runtime.parent.is_symlink() or (root is not None and runtime.parent != common_dir(root) / FOLDER):
        raise ValueError('Python check runtime belongs to a different repository')
    return runtime


def environment(runtime, path=None):
    inherited_path = path if path is not None else os.environ.get('PATH') or os.defpath
    if (os.pathsep in str(runtime) or
            any(not Path(directory).is_absolute() for directory in inherited_path.split(os.pathsep))):
        raise ValueError('Relative or empty PATH entry cannot be bound to the Python check runtime')
    return {'PATH': str(runtime / 'bin') + os.pathsep + inherited_path,
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def inspect(descriptor, budget=None, *, root=None, path=None):
    """Revalidate isolation before a check can use the runtime or its evidence."""
    runtime = location(descriptor, root)
    host_matches(descriptor, budget)
    config_digest = structure(runtime, descriptor, budget)
    return {'path_prefix': str(runtime / 'bin'), 'fingerprint': digest([descriptor, config_digest]),
            'environment': environment(runtime, path)}


def native_python(argv):
    try:
        return subprocess.run(argv, env={'PATH': os.defpath}, check=True, capture_output=True, text=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise Halt('Python check runtime setup or isolation probe failed') from exc


def install(descriptor, *, root=None):
    """Create the runtime only on explicit setup, with the caller's supervisor lock."""
    runtime = location(descriptor, root)
    environment(runtime)
    if os.path.lexists(runtime):
        return inspect(descriptor, root=root)
    host_matches(descriptor, None)
    runtime.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    python_names = {'python', 'python3', versioned_python(descriptor)}
    with tempfile.TemporaryDirectory(prefix='.new-', dir=runtime.parent) as temporary:
        stage = Path(temporary) / 'runtime'
        builder = 'import sys, venv; venv.EnvBuilder(with_pip=False, symlinks=True).create(sys.argv[1])'
        native_python([descriptor['launcher'], '-I', '-S', '-c', builder, str(stage)])
        for path in (stage / 'bin').iterdir():
            if path.name not in python_names:
                path.unlink()
        structure(stage, descriptor, None)
        probe = ('import json, site, sys, sysconfig; '
                 'print(json.dumps([sys.prefix, sys.base_prefix, sysconfig.get_path("stdlib"), '
                 'site.ENABLE_USER_SITE, site.getsitepackages()]))')
        result = native_python([str(stage / 'bin' / 'python3'), '-I', '-c', probe])
        prefix, base, stdlib, user_site, system_sites = json.loads(result.stdout)
        if (Path(prefix).resolve() != stage or str(Path(base).resolve()) != descriptor['base_prefix'] or
                str(Path(stdlib).resolve()) != descriptor['stdlib'] or user_site is not False or
                not system_sites or any(stage not in Path(path).resolve().parents for path in system_sites)):
            raise Halt('Python check runtime isolation probe failed')
        if os.path.lexists(runtime):
            raise Halt('Python check runtime appeared during setup')
        stage.rename(runtime)
    return inspect(descriptor, root=root)
