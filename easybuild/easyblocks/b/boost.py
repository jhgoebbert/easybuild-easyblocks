##
# Copyright 2009-2021 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
EasyBuild support for Boost, implemented as an easyblock

@author: Stijn De Weirdt (Ghent University)
@author: Dries Verdegem (Ghent University)
@author: Kenneth Hoste (Ghent University)
@author: Pieter De Baets (Ghent University)
@author: Jens Timmerman (Ghent University)
@author: Ward Poelmans (Ghent University)
@author: Petar Forai (IMP/IMBA)
@author: Luca Marsella (CSCS)
@author: Guilherme Peretti-Pezzi (CSCS)
@author: Joachim Hein (Lund University)
@author: Michele Dolfi (ETH Zurich)
@author: Simon Branford (University of Birmingham)
"""
from distutils.version import LooseVersion
import fileinput
import glob
import os
import re
import sys

import easybuild.tools.toolchain as toolchain
from easybuild.framework.easyblock import EasyBlock
from easybuild.framework.easyconfig import CUSTOM
from easybuild.tools.build_log import EasyBuildError, print_warning
from easybuild.tools.config import ERROR
from easybuild.tools.filetools import apply_regex_substitutions, copy, mkdir, symlink, which, write_file
from easybuild.tools.modules import get_software_root, get_software_version
from easybuild.tools.run import run_cmd
from easybuild.tools.systemtools import AARCH64, POWER, UNKNOWN
from easybuild.tools.systemtools import get_cpu_architecture, get_glibc_version, get_shared_lib_ext


class EB_Boost(EasyBlock):
    """Support for building Boost."""

    def __init__(self, *args, **kwargs):
        """Initialize Boost-specific variables."""
        super(EB_Boost, self).__init__(*args, **kwargs)

        self.objdir = None

        self.pyvers = []

        if LooseVersion(self.version) >= LooseVersion("1.71.0"):
            self.bjamcmd = 'b2'
        else:
            self.bjamcmd = 'bjam'

    @staticmethod
    def extra_options():
        """Add extra easyconfig parameters for Boost."""
        extra_vars = {
            'boost_mpi': [False, "Build mpi boost module", CUSTOM],
            'boost_multi_thread': [None, "Build boost with multi-thread option (DEPRECATED)", CUSTOM],
            'toolset': [None, "Toolset to use for Boost configuration ('--with-toolset' for bootstrap.sh)", CUSTOM],
            'build_toolset': [None, "Toolset to use for Boost compilation "
                                    "('toolset' for b2, default calculated from toolset)", CUSTOM],
            'mpi_launcher': [None, "Launcher to use when running MPI regression tests", CUSTOM],
            'only_python_bindings': [False, "Only install Boost.Python library providing Python bindings", CUSTOM],
            'use_glibcxx11_abi': [None, "Use the GLIBCXX11 ABI", CUSTOM],
        }
        return EasyBlock.extra_options(extra_vars)

    def patch_step(self):
        """Patch Boost source code before building."""
        super(EB_Boost, self).patch_step()

        # TIME_UTC is also defined in recent glibc versions, so we need to rename it for old Boost versions (<= 1.49)
        glibc_version = get_glibc_version()
        old_glibc = glibc_version is not UNKNOWN and LooseVersion(glibc_version) > LooseVersion("2.15")
        if old_glibc and LooseVersion(self.version) <= LooseVersion("1.49.0"):
            self.log.info("Patching because the glibc version is too new")
            files_to_patch = ["boost/thread/xtime.hpp"] + glob.glob("libs/interprocess/test/*.hpp")
            files_to_patch += glob.glob("libs/spirit/classic/test/*.cpp") + glob.glob("libs/spirit/classic/test/*.inl")
            for patchfile in files_to_patch:
                try:
                    for line in fileinput.input("%s" % patchfile, inplace=1, backup='.orig'):
                        line = re.sub(r"TIME_UTC", r"TIME_UTC_", line)
                        sys.stdout.write(line)
                except IOError as err:
                    raise EasyBuildError("Failed to patch %s: %s", patchfile, err)

    def prepare_step(self, *args, **kwargs):
        """Prepare build environment."""

        super(EB_Boost, self).prepare_step(*args, **kwargs)

        # keep track of Python version(s) used during installation,
        # so we can perform a complete sanity check
        if get_software_root('Python'):
            self.pyvers.append(get_software_version('Python'))

    def configure_step(self):
        """Configure Boost build using custom tools"""

        # boost_multi_thread is deprecated
        if boost_multi_thread is not None:
            self.log.deprecated("boost_multi_thread has been deprecated, "
                                "we always build both single and multi threading libraries.")

        # mpi sanity check
        if self.cfg['boost_mpi'] and not self.toolchain.options.get('usempi', None):
            raise EasyBuildError("When enabling building boost_mpi, also enable the 'usempi' toolchain option.")

        # create build directory (Boost doesn't like being built in source dir)
        self.objdir = os.path.join(self.builddir, 'obj')
        mkdir(self.objdir)

        # generate config depending on compiler used
        toolset = self.cfg['toolset']
        if toolset is None:
            if self.toolchain.comp_family() == toolchain.INTELCOMP:
                toolset = 'intel-linux'
            elif self.toolchain.comp_family() == toolchain.GCC:
                toolset = 'gcc'
            else:
                raise EasyBuildError("Unknown compiler used, don't know what to specify to --with-toolset, aborting.")

        cmd = "%s ./bootstrap.sh --with-toolset=%s --prefix=%s %s"
        tup = (self.cfg['preconfigopts'], toolset, self.objdir, self.cfg['configopts'])
        run_cmd(cmd % tup, log_all=True, simple=True)

        # Use build_toolset if specified or the bootstrap toolset without the OS suffix
        self.toolset = self.cfg['build_toolset'] or re.sub('-linux$', '', toolset)

        user_config = []

        # Explicitely set the compiler path to avoid B2 checking some standard paths like /opt
        cxx = os.getenv('CXX')
        if cxx:
            cxx = which(cxx, on_error=ERROR)
            # Remove default toolset config which may lead to duplicate toolsets (e.g. for intel-linux)
            apply_regex_substitutions('project-config.jam', [('using %s ;' % toolset, '')])
            # Add our toolset config with no version and full path to compiler
            user_config.append("using %s : : %s ;" % (self.toolset, cxx))

        if self.cfg['boost_mpi']:

            # configure the boost mpi module
            # http://www.boost.org/doc/libs/1_47_0/doc/html/mpi/getting_started.html
            # let Boost.Build know to look here for the config file

            # Check if using a Cray toolchain and configure MPI accordingly
            if self.toolchain.toolchain_family() == toolchain.CRAYPE:
                if self.toolchain.PRGENV_MODULE_NAME_SUFFIX == 'gnu':
                    craympichdir = os.getenv('CRAY_MPICH2_DIR')
                    craygccversion = os.getenv('GCC_VERSION')
                    # We configure the gcc toolchain below, so make sure the EC doesn't use another toolset
                    if self.toolset != 'gcc':
                        raise EasyBuildError("For the cray toolchain the 'gcc' toolset must be used.")
                    # Remove the previous "using gcc" line add above (via self.toolset) if present
                    user_config = [x for x in user_config if not x.startswith('using gcc :')]
                    user_config.extend([
                        'local CRAY_MPICH2_DIR =  %s ;' % craympichdir,
                        'using gcc ',
                        ': %s' % craygccversion,
                        ': CC ',
                        ': <compileflags>-I$(CRAY_MPICH2_DIR)/include ',
                        r'  <linkflags>-L$(CRAY_MPICH2_DIR)/lib \ ',
                        '; ',
                        'using mpi ',
                        ': CC ',
                        ': <find-shared-library>mpich ',
                        ': %s' % self.cfg['mpi_launcher'],
                        ';',
                        '',
                    ])
                else:
                    raise EasyBuildError("Bailing out: only PrgEnv-gnu supported for now")
            else:
                user_config.append("using mpi : %s ;" % os.getenv("MPICXX"))

        write_file('user-config.jam', '\n'.join(user_config), append=True)

    def build_boost_variant(self, bjamoptions, paracmd):
        """Build Boost library with specified options for bjam."""
        # build with specified options
        cmd = "%s ./%s %s %s %s" % (self.cfg['prebuildopts'], self.bjamcmd, bjamoptions, paracmd, self.cfg['buildopts'])
        run_cmd(cmd, log_all=True, simple=True)
        # install built Boost library
        cmd = "%s ./%s %s install %s %s" % (
            self.cfg['preinstallopts'], self.bjamcmd, bjamoptions, paracmd, self.cfg['installopts'])
        run_cmd(cmd, log_all=True, simple=True)
        # clean up before proceeding with next build
        run_cmd("./%s %s --clean-all" % (self.bjamcmd, bjamoptions), log_all=True, simple=True)

    def build_step(self):
        """Build Boost with bjam tool."""

        self.bjamoptions = " --prefix=%s --user-config=user-config.jam" % self.objdir
        if 'toolset=' not in self.cfg['buildopts']:
            self.bjamoptions += " toolset=" + self.toolset

        cxxflags = os.getenv('CXXFLAGS')
        # only disable -D_GLIBCXX_USE_CXX11_ABI if use_glibcxx11_abi was explicitly set to False
        # None value is the default, which corresponds to default setting (=1 since GCC 5.x)
        if self.cfg['use_glibcxx11_abi'] is not None:
            cxxflags += ' -D_GLIBCXX_USE_CXX11_ABI='
            if self.cfg['use_glibcxx11_abi']:
                cxxflags += '1'
            else:
                cxxflags += '0'
        if cxxflags is not None:
            self.bjamoptions += " cxxflags='%s'" % cxxflags
        ldflags = os.getenv('LDFLAGS')
        if ldflags is not None:
            self.bjamoptions += " linkflags='%s'" % ldflags

        # specify path for bzip2/zlib if module is loaded
        for lib in ["bzip2", "zlib"]:
            libroot = get_software_root(lib)
            if libroot:
                self.bjamoptions += " -s%s_INCLUDE=%s/include" % (lib.upper(), libroot)
                self.bjamoptions += " -s%s_LIBPATH=%s/lib" % (lib.upper(), libroot)

        self.paracmd = ''
        if self.cfg['parallel']:
            self.paracmd = "-j %s" % self.cfg['parallel']

        if self.cfg['only_python_bindings']:
            # magic incantation to only install Boost Python bindings is... --with-python
            # see http://boostorg.github.io/python/doc/html/building/installing_boost_python_on_your_.html
            self.bjamoptions += " --with-python"

        self.log.info("Building boost with single and multi threading")
        self.bjamoptions += " threading=single,multi --layout=tagged"

        if self.cfg['boost_mpi']:
            self.log.info("Building boost_mpi library")
            mpi_bjamoptions = " --with-mpi"
            self.build_boost_variant(self.bjamoptions + mpi_bjamoptions, self.paracmd)

        self.log.info("Building boost libraries")
        # build with specified options
        cmd = "%s ./%s %s %s %s" % (
            self.cfg['prebuildopts'], self.bjamcmd, self.bjamoptions, self.paracmd, self.cfg['buildopts'])
        run_cmd(cmd, log_all=True, simple=True)

    def install_step(self):
        """Install Boost by copying files to install dir."""

        # install boost libraries
        self.log.info("Installing boost libraries")

        cmd = "%s ./%s %s install %s %s" % (
            self.cfg['preinstallopts'], self.bjamcmd, self.bjamoptions, self.paracmd, self.cfg['installopts'])
        run_cmd(cmd, log_all=True, simple=True)

        self.log.info("Copying %s to installation dir %s", self.objdir, self.installdir)
        if self.cfg['only_python_bindings'] and 'Python' in self.cfg['multi_deps'] and self.iter_idx > 0:
            self.log.info("Main installation should already exist, only copying over missing Python libraries.")
            copy(glob.glob(os.path.join(self.objdir, 'lib', 'libboost_python*')), os.path.join(self.installdir, 'lib'),
                 symlinks=True)
        else:
            copy(glob.glob(os.path.join(self.objdir, '*')), self.installdir, symlinks=True)

        # Link tagged multi threaded libs as the default libs
        lib_glob = 'lib*-mt-*.*'
        mt_replace = re.compile(r'-mt[^.]*\.')
        for source_lib in glob.glob(os.path.join(self.installdir, 'lib', lib_glob)):
            target_lib = mt_replace.sub('.', source_lib)
            symlink(os.path.basename(source_lib), target_lib, use_abspath_source=False)

    def sanity_check_step(self):
        """Custom sanity check for Boost."""
        shlib_ext = get_shared_lib_ext()

        custom_paths = {
            'files': [],
            'dirs': ['include/boost']
        }
        if self.cfg['only_python_bindings']:
            for pyver in self.pyvers:
                pymajorver = pyver.split('.')[0]
                pyminorver = pyver.split('.')[1]
                if LooseVersion(self.version) >= LooseVersion("1.67.0"):
                    suffix = '%s%s' % (pymajorver, pyminorver)
                elif int(pymajorver) >= 3:
                    suffix = pymajorver
                else:
                    suffix = ''
                custom_paths['files'].append(os.path.join('lib', 'libboost_python%s.%s' % (suffix, shlib_ext)))

        else:
            custom_paths['files'].append(os.path.join('lib', 'libboost_system.%s' % shlib_ext))

            lib_mt_suffix = '-mt'
            # MT libraries gained an extra suffix from v1.69.0 onwards
            if LooseVersion(self.version) >= LooseVersion("1.69.0"):
                if get_cpu_architecture() == AARCH64:
                    lib_mt_suffix += '-a64'
                elif get_cpu_architecture() == POWER:
                    lib_mt_suffix += '-p64'
                else:
                    lib_mt_suffix += '-x64'

            custom_paths['files'].append(os.path.join('lib', 'libboost_thread%s.%s' % (lib_mt_suffix, shlib_ext)))

            if self.cfg['boost_mpi']:
                custom_paths['files'].append(os.path.join('lib', 'libboost_mpi.%s' % shlib_ext))
                custom_paths['files'].append(os.path.join('lib', 'libboost_mpi%s.%s' % (lib_mt_suffix, shlib_ext)))

        super(EB_Boost, self).sanity_check_step(custom_paths=custom_paths)

    def make_module_extra(self):
        """Set up a BOOST_ROOT environment variable to e.g. ease Boost handling by cmake"""
        txt = super(EB_Boost, self).make_module_extra()
        if not self.cfg['only_python_bindings']:
            txt += self.module_generator.set_environment('BOOST_ROOT', self.installdir)
        return txt
