"""
engine.py: Communicate with QM or MM software packages for energy/gradient info

Copyright 2016-2020 Regents of the University of California and the Authors

Authors: Lee-Ping Wang, Chenchen Song

Contributors: Yudong Qiu, Daniel G. A. Smith, Sebastian Lee, Qiming Sun,
Alberto Gobbi, Josh Horton

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
this list of conditions and the following disclaimer in the documentation
and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
may be used to endorse or promote products derived from this software
without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
"""

from __future__ import print_function, division

import shutil
import subprocess
from collections import OrderedDict
from copy import deepcopy
import xml.etree.ElementTree as ET

import numpy as np
import re
import os

from .molecule import Molecule
from .nifty import bak, eqcgmx, fqcgmx, bohr2ang, logger, getWorkQueue, queue_up_src_dest, splitall
from .errors import EngineError, Psi4EngineError, QChemEngineError, TeraChemEngineError, ConicalIntersectionEngineError, \
    OpenMMEngineError, GromacsEngineError, MolproEngineError, QCEngineAPIEngineError

#=============================#
#| Useful TeraChem functions |#
#=============================#

def edit_tcin(fin=None, fout=None, options=None, defaults=None, reqxyz=True, ignore_sections=True):
    """
    Parse, modify, and/or create a TeraChem input file.

    Parameters
    ----------
    fin : str, optional
        Name of the TeraChem input file to be read
    fout : str, optional
        Name of the TeraChem output file to be written, if desired
    options : dict, optional
        Dictionary of options to overrule TeraChem input file. Pass None as value to delete a key.
    defaults : dict, optional
        Dictionary of options to add to the end
    reqxyz : bool, optional
        Require .xyz file to be present in the current folder
    ignore_sections : bool, optional
        Do not parse any blocks delimited by dollar signs (not copied to output and not returned)

    Returns
    -------
    dictionary
        Keys mapped to values as strings.  Certain keys will be changed to integers (e.g. charge, spinmult).
        Keys are standardized to lowercase.
    """
    if defaults is None:
        defaults = {}
    if options is None:
        options = {}
    if not ignore_sections:
        raise RuntimeError("Currently only ignore_constraints=True is supported")
    intkeys = ['charge', 'spinmult']
    Answer = OrderedDict()
    # Read from the input if provided
    if fin is not None:
        tcin_dirname = os.path.dirname(os.path.abspath(fin))
        section_mode = False
        for line in open(fin).readlines():
            line = line.split("#")[0].strip()
            if len(line) == 0: continue
            if line == '$end':
                section_mode = False
                continue
            elif line.startswith("$"):
                section_mode = True
            if section_mode : continue
            if line == 'end': break
            s = line.split(' ', 1)
            k = s[0].lower()
            try:
                v = s[1].strip()
            except IndexError:
                raise RuntimeError("%s contains an error on the following line:\n%s" % (fin, line))
            if k == 'coordinates' and reqxyz:
                if not os.path.exists(os.path.join(tcin_dirname, v.strip())):
                    raise RuntimeError("TeraChem coordinate file does not exist")
            if k in intkeys:
                v = int(v)
            if k in Answer:
                raise RuntimeError("Found duplicate key in TeraChem input file: %s" % k)
            Answer[k] = v
    # Replace existing keys with ones from options
    for k, v in options.items():
        Answer[k] = v
    # Append defaults to the end
    for k, v in defaults.items():
        if k not in Answer.keys():
            Answer[k] = v
    for k, v in Answer.items():
        if v is None:
            del Answer[k]
    # Print to the output if provided
    havekeys = []
    if fout is not None:
        with open(fout, 'w') as f:
            # If input file is provided, try to preserve the formatting
            if fin is not None:
                for line in open(fin).readlines():
                    # Find if the line contains a key
                    haveKey = False
                    uncomm = line.split("#", 1)[0].strip()
                    # Don't keep anything past the 'end' keyword
                    if uncomm.lower() == 'end': break
                    if len(uncomm) > 0:
                        haveKey = True
                        comm = line.split("#", 1)[1].replace('\n','') if len(line.split("#", 1)) == 2 else ''
                        s = line.split(' ', 1)
                        w = re.findall('[ ]+',uncomm)[0]
                        k = s[0].lower()
                        if k in Answer:
                            line_out = k + w + str(Answer[k]) + comm
                            havekeys.append(k)
                        else:
                            line_out = line.replace('\n', '')
                    else:
                        line_out = line.replace('\n', '')
                    print(line_out, file=f)
            for k, v in Answer.items():
                if k not in havekeys:
                    print("%-15s %s" % (k, str(v)), file=f)
    return Answer

def set_tcenv():
    if 'TeraChem' not in os.environ:
        raise RuntimeError('Please set TeraChem environment variable')
    TCHome = os.environ['TeraChem']
    os.environ['PATH'] = os.path.join(TCHome,'bin')+":"+os.environ['PATH']
    os.environ['LD_LIBRARY_PATH'] = os.path.join(TCHome,'lib')+":"+os.environ['LD_LIBRARY_PATH']

def load_tcin(f_tcin):
    tcdef = OrderedDict()
    tcdef['convthre'] = "3.0e-6"
    tcdef['threall'] = "1.0e-13"
    tcdef['scf'] = "diis+a"
    tcdef['maxit'] = "50"
    # tcdef['dftgrid'] = "1"
    # tcdef['precision'] = "mixed"
    # tcdef['threspdp'] = "1.0e-8"
    tcin = edit_tcin(fin=f_tcin, options={'run':'gradient'}, defaults=tcdef)
    return tcin

#====================================#
#| Classes for external codes used  |#
#| to calculate energy and gradient |#
#====================================#

class Engine(object):
    def __init__(self, molecule):
        if len(molecule) != 1:
            raise RuntimeError('Please pass only length-1 molecule objects to engine creation')
        self.M = deepcopy(molecule)
        self.stored_calcs = OrderedDict()

    # def __deepcopy__(self, memo):
    #     return copy(self)

    def calc(self, coords, dirname, readfiles=False):
        """
        Top-level method for a single-point calculation. 
        Calculation will be skipped if results are contained in the hash table, 
        and optionally, can skip calculation if output exists on disk (which is 
        useful in the case of restarting a crashed Hessian calculation)
        
        Parameters
        ----------
        coords : np.array
            1-dimensional array of shape (3*N_atoms) containing atomic coordinates in Bohr
        dirname : str
            Relative path containing calculation files
        readfiles : bool, default=False
            If valid calculation output files exist in dirname, read the results instead of
            running a new calculation

        Returns
        -------
        result : dict
            Dictionary containing results:
            result['energy'] = float
                Energy in atomic units
            result['gradient'] = np.array
                1-dimensional array of same shape as coords, containing nuclear gradients in a.u.
            result['s2'] = float
                Optional output containing expectation value of <S^2> operator, used in
                crossing point optimizations
        """
        coord_hash = hash(coords.tostring())
        if coord_hash in self.stored_calcs:
            result = self.stored_calcs[coord_hash]['result']
        else:
            # If the readfiles flag is set to True, then attempt to read the
            # result from the temp-folder, then skip the calculation if successful.
            read_success = False
            if readfiles and os.path.exists(dirname) and hasattr(self, 'read_result'):
                try:
                    result = self.read_result(dirname)
                    read_success = True
                except:
                    logger.info("Failed to read output from %s, recalculating\n" % dirname)
            if not read_success:
                if not os.path.exists(dirname): os.makedirs(dirname)
                result = self.calc_new(coords, dirname)
            self.stored_calcs[coord_hash] = {'coords':coords, 'result':result}
        return result

    def clearCalcs(self):
        self.stored_calcs = OrderedDict()

    def calc_new(self, coords, dirname):
        raise NotImplementedError("Not implemented for the base class")

    def calc_wq(self, coords, dirname, readfiles=False):
        """
        Top-level method for submitting a single-point calculation using Work Queue. 
        Different from calc(), this method does not return results, because the control
        flow involves submitting calculations to WQ and gathering data after calculations
        are complete.
        
        Calculation will be skipped if results are contained in the hash table, 
        and optionally, can skip calculation if output exists on disk (which is 
        useful in the case of restarting a crashed Hessian calculation)
        
        Parameters
        ----------
        coords : np.array
            1-dimensional array of shape (3*N_atoms) containing atomic coordinates in Bohr
        dirname : str
            Relative path containing calculation files
        readfiles : bool, default=False
            If valid calculation output files exist in dirname, read the results instead of
            running a new calculation
        """
        coord_hash = hash(coords.tostring())
        if coord_hash in self.stored_calcs:
            return
        else:
            # If the readfiles flag is set to True, then attempt to read the
            # result from the temp-folder, then skip the calculation if successful.
            read_success = False
            if readfiles and os.path.exists(dirname) and hasattr(self, 'read_result'):
                try:
                    result = self.read_result(dirname)
                    read_success = True
                except:
                    logger.info("Tried but failed to read results from %s\nRunning new calculation\n" % dirname)
            if not read_success:
                self.calc_wq_new(coords, dirname)

    def calc_wq_new(self, coords, dirname):
        raise NotImplementedError("Work Queue is not implemented for this class")

    def read_wq(self, coords, dirname):
        """
        Read Work Queue results after all jobs are completed.

        Parameters
        ----------
        coords : np.array
            1-dimensional array of shape (3*N_atoms) containing atomic coordinates in Bohr.
            Used for retrieving results from hash table (in which case calc was not submitted)
        dirname : str
            Relative path containing calculation files

        Returns
        -------
        result : dict
            Dictionary containing results:
            result['energy'] = float
                Energy in atomic units
            result['gradient'] = np.array
                1-dimensional array of same shape as coords, containing nuclear gradients in a.u.
            result['s2'] = float
                Optional output containing expectation value of <S^2> operator, used in
                crossing point optimizations
        """
        coord_hash = hash(coords.tostring())
        if coord_hash in self.stored_calcs:
            result = self.stored_calcs[coord_hash]['result']
        else:
            if not os.path.exists(dirname):
                raise RuntimeError("In read_wq, %s doesn't exist" % dirname)
            result = self.read_result(dirname)
            self.stored_calcs[coord_hash] = {'coords':coords, 'result':result}
        return result

    def read_wq_new(self, coords, dirname):
        raise NotImplementedError("Work Queue is not implemented for this class")

    def number_output(self, dirname, calcNum):
        return

class Blank(Engine):
    """
    Always return zero energy and gradient.
    """
    def __init__(self, molecule):
        super(Blank, self).__init__(molecule)

    def calc_new(self, coords, dirname):
        energy = 0.0
        gradient = np.zeros(len(coords), dtype=float)
        return {'energy':energy, 'gradient':gradient}

class Zmachine(Engine):
    """
    Run a prototypical Zmachine energy and gradient calculation.
    """
    def __init__(self, molecule=None):
        self.basis = np.eye(3)
        # format num_points : ([c_{+num_points/2}, ..., c_{1}], denom)
        self.fd_formulae = {2 : ([1.0], 2.0), 4 : ([-1.0, 8.0], 12.0), 6 : ([1.0, -9.0, 45.0], 60.0) }
        # molecule.py can not parse psi4 input yet, so we use self.load_psi4_input() as a walk around
        if molecule is None:
            # create a fake molecule
            molecule = Molecule()
            molecule.elem = ['H']
            molecule.xyzs = [[[0,0,0]]]
        super(Zmachine, self).__init__(molecule)

    def load_zmachine_input(self, zmachinein):
        """ Parse a JSON input file """
        import json
        coords = []
        elems = []
        with open(zmachinein, 'r') as f:
            s = f.read()
            input_dict = json.loads(s)
        self.M = Molecule()
        self.M.elem = input_dict['atoms']['elements']['symbols']
        self.M.xyzs = [np.array(input_dict['atoms']['coords']['3d'], dtype=np.float64).reshape(-1, 3)]
        self.psi4_options = {}
        self.psi4_options['basis'] = input_dict.get('basis', 'sto-3g')
        self.psi4_options['scf_type'] = input_dict.get('scf_type', 'pk')
        self.psi4_options['e_convergence'] = input_dict.get('e_convergence', 11)
        self.psi4_options['d_convergence'] = input_dict.get('d_convergence', 11)
        self.fd_options = {}
        self.fd_options['npoint'] = input_dict.get('npoint', 2)
        self.fd_options['d'] = input_dict.get('d', 0.01)

        print("psi4_options: ", self.psi4_options)
        print("fd_options: ", self.fd_options)
        print(self.M.elem)
        print(self.M.xyzs)

    def request_energy(self, geom, dirname):
        """ geom is [[np.array, ...]] 
        """
        import psi4
        if not os.path.exists(dirname): os.makedirs(dirname)
        psi4out = os.path.join(dirname, 'output.dat')
        psi4.core.set_output_file(psi4out, True)
        psi4.set_options(self.psi4_options)
        #create a str representations of molecular geometries and run Psi4
        energies = []
        for at_block in geom:
            at_block_energy = []
            for g in at_block:
                g_str = ''
                for atom, coords in zip(self.M.elem, g):
                    g_str += "{0} {1:13.6f} {2:13.6} {3:13.6}\n".format(atom, coords[0], coords[1], coords[2])
                tmp_mol = psi4.geometry(g_str)
                at_block_energy.append(psi4.energy('scf', mol=tmp_mol))
            energies.append(at_block_energy)

        return energies

    def compute_gradient(self, en):
        assert self.fd_options['npoint'] % 2 == 0 and self.fd_options['npoint'] in self.fd_formulae
        npoint = self.fd_options['npoint']
        en = np.array(en)
        en = en.reshape((-1, 3, npoint))
        disp = self.fd_options['d']
        # apply fd stencil to calculate derivatives
        num = list(self.fd_formulae[npoint][0])
        num.extend([-i for i in reversed(num)])
        denom = self.fd_formulae[npoint][1] * disp
        stencil = np.array(num) / denom
        return np.einsum("ijk,k->ij", en, stencil)

    def calc_new(self, coords, dirname):
        # Convert coordinates back to the xyz file
        self.M.xyzs[0] = coords.reshape(-1, 3) * bohr2ang # in angstrom!!
        geometries = []
        npoint_one_way = self.fd_options['npoint'] // 2
        disp  = self.fd_options['d']
        for at in range(len(self.M.elem)):
            at_disp = []
            for c in range(3):
                for sc in range(npoint_one_way, -npoint_one_way-1, -1):
                    if sc == 0:
                        continue
                    else:
                        g = np.copy(self.M.xyzs[0])
                        g[at] += sc * disp * self.basis[c]
                        at_disp.append(g)
            geometries.append(at_disp)

        energies = self.request_energy(geometries,dirname)
        ref_gradient = self.compute_gradient(energies)
        ref_energy = self.request_energy([[self.M.xyzs[0]]], dirname)[0][0] # ugly...

        print("ref_gradient: ", ref_gradient)

        return {'energy':ref_energy, 'gradient':ref_gradient.ravel()}

class TeraChem(Engine):
    """
    Run a TeraChem energy and gradient calculation.
    """
    def __init__(self, molecule, tcin, dirname=None):
        self.tcin = tcin.copy()
        if 'scrdir' in self.tcin:
            self.scr = self.tcin['scrdir']
        else:
            self.scr = 'scr'
        if 'guess' in self.tcin:
            guessVal = self.tcin['guess'].split()
            if guessVal[0] == 'frag':
                self.guessMode = 'frag'
                self.fragFile = guessVal[1]
                if not os.path.exists(self.fragFile):
                    raise TeraChemEngineError('%s fragment file is missing' % self.fragFile)
            else:
                self.guessMode = 'file'
                for f in guessVal:
                    if not os.path.exists(f):
                        raise TeraChemEngineError('%s guess file is missing' % f)
        else:
            self.guessMode = 'none'
        # Management of QM/MM: Read qmindices and 
        # store locations of prmtop and qmindices files,
        self.qmmm = 'qmindices' in tcin
        if self.qmmm:
            if not os.path.exists(tcin['coordinates']):
                raise RuntimeError("TeraChem QM/MM coordinate file does not exist")
            if not os.path.exists(tcin['prmtop']):
                raise RuntimeError("TeraChem QM/MM prmtop file does not exist")
            if not os.path.exists(tcin['qmindices']):
                raise RuntimeError("TeraChem QM/MM qmindices file does not exist")
            self.qmindices_name = os.path.abspath(tcin['qmindices'])
            self.prmtop_name = os.path.abspath(tcin['prmtop'])
            self.qmindices = [int(i.split()[0]) for i in open(self.qmindices_name).readlines()]
            self.M_full = Molecule(tcin['coordinates'], ftype='inpcrd', build_topology=False)
        if dirname: self.prep_temp_folder(dirname)
        super(TeraChem, self).__init__(molecule)

    def prep_temp_folder(self, dirname):
        # Clean up the temporary folder.
        if os.path.exists(dirname):
            # Remove existing scratch files in ./run.tmp/scr to avoid confusion
            for f in ['c0', 'ca0', 'cb0']:
                if os.path.exists(os.path.join(dirname, self.scr, f)):
                    os.remove(os.path.join(dirname, self.scr, f))
        
    def manage_guess(self, dirname):
        """
        Management of guess files in TeraChem calculations.
        This function make sure the correct guess files are in the temp-folder
        given by "dirname" and sets the corresponding options in self.tcin.

        Returns
        -------
        fileNames : list
            The list of guess files that the TeraChem calculation will require,
            whether it is a MO coefficient file or a text file specifying fragments.
        """
        # These are files that may be produced in a previous energy/grad calc
        # (This may be changed if non-single-reference calcs start to be used)
        unrestricted = self.tcin['method'][0] == 'u'
        if unrestricted:
            scrFiles = ['ca0', 'cb0']
        else:
            scrFiles = ['c0']
        if self.tcin.get('casscf', 'no').lower() == 'yes':
            is_casscf = True
            scrFiles += ['c0.casscf']
        else: is_casscf = False
        # Copy fragment guess file if applicable. It will be used in every energy/grad calc
        if self.guessMode == 'frag':
            shutil.copy2(self.fragFile, dirname)
            return [self.fragFile]
        # If guess is not set and orbital files are in temp/scr from a previous energy/grad calc,
        # then set guess mode to use files.
        if self.guessMode == 'none':
            guessFiles = []
            for f in scrFiles:
                if os.path.exists(os.path.join(dirname, self.scr, f)):
                    guessFiles.append(f)
            if guessFiles:
                self.tcin['guess'] = ' '.join([f for f in guessFiles if 'casscf' not in f])
                if 'c0.casscf' in guessFiles and is_casscf: self.tcin['casguess'] = 'c0.casscf'
                self.guessMode = 'file'
            else:
                return []
        # If using guess files (including from the above if-block), copy the guess files into the temp-dir.
        # Use files in temp/scr if they exist; otherwise, use files in the base dir.
        if self.guessMode != 'file':
            raise TeraChemEngineError("Guess mode should be 'file' at this point in the code: currently %s" % self.guessMode)
        guessFiles = self.tcin['guess'].split()
        for f in guessFiles:
            if f in scrFiles and os.path.exists(os.path.join(dirname, self.scr, f)):
                shutil.copy2(os.path.join(dirname, self.scr, f), os.path.join(dirname, f))
            elif not os.path.exists(os.path.join(dirname, f)):
                shutil.copy2(f, dirname)
            if not os.path.exists(os.path.join(dirname, f)):
                raise TeraChemEngineError("%s guess file is missing and this code shouldn't be called" % f)
        if is_casscf:
            f = 'c0.casscf'
            if os.path.exists(os.path.join(dirname, self.scr, f)):
                shutil.copy2(os.path.join(dirname, self.scr, f), os.path.join(dirname, f))
            elif not os.path.exists(os.path.join(dirname, f)):
                shutil.copy2(f, dirname)
            if not os.path.exists(os.path.join(dirname, f)):
                raise TeraChemEngineError("%s guess file is missing and this code shouldn't be called" % f)
        # When guess files are provided, turn off purify and mix.
        if 'purify' not in self.tcin: self.tcin['purify'] = 'no'
        if 'mixguess' not in self.tcin: self.tcin['mixguess'] = "0.0"
        return guessFiles

    def calc_new(self, coords, dirname):
        # Ensure guess files are in the correct locations
        self.manage_guess(dirname)
        # Copy QMMM files to the correct locations
        if self.qmmm:
            shutil.copy2(self.qmindices_name, dirname)
            shutil.copy2(self.prmtop_name, dirname)
        # Set other needed options
        start_xyz = 'start.rst7' if self.qmmm else 'start.xyz'
        self.tcin['coordinates'] = start_xyz
        self.tcin['run'] = 'gradient'
        # Write the TeraChem input file
        edit_tcin(fout="%s/run.in" % dirname, options=self.tcin)
        # Back up any existing output files
        # Commented out (should be enabled during debuggin')
        # bak('run.out', cwd=dirname, start=0)
        # bak(start_xyz, cwd=dirname, start=0)
        # Convert coordinates back to the xyz file
        self.M.xyzs[0] = coords.reshape(-1, 3) * bohr2ang
        if self.qmmm:
            self.M_full.xyzs[0][self.qmindices, :] = self.M.xyzs[0]
            self.M_full[0].write(os.path.join(dirname, start_xyz), ftype='inpcrd')
        else:
            self.M[0].write(os.path.join(dirname, start_xyz))
        # Run TeraChem
        subprocess.check_call('terachem run.in > run.out', cwd=dirname, shell=True)
        # Extract energy and gradient
        result = self.read_result(dirname)
        return result

    def calc_wq_new(self, coords, dirname):
        wq = getWorkQueue()
        if not os.path.exists(dirname): os.makedirs(dirname)
        scrdir = os.path.join(dirname, self.scr)
        if not os.path.exists(scrdir): os.makedirs(scrdir)
        guessfnms = self.manage_guess(dirname)
        start_xyz = 'start.rst7' if self.qmmm else 'start.xyz'
        self.tcin['coordinates'] = start_xyz
        self.tcin['run'] = 'gradient'
        # For queueing up jobs, delete GPU key and let the worker decide
        self.tcin['gpus'] = None
        tcopts = edit_tcin(fout="%s/run.in" % dirname, options=self.tcin)
        # Convert coordinates back to the xyz file
        self.M.xyzs[0] = coords.reshape(-1, 3) * bohr2ang
        if self.qmmm:
            self.M_full.xyzs[0][self.qmindices, :] = self.M.xyzs[0]
            self.M_full[0].write(os.path.join(dirname, start_xyz), ftype='inpcrd')
        else:
            self.M[0].write(os.path.join(dirname, start_xyz))
        # Specify WQ input and output files
        in_files = [('%s/run.in' % dirname, 'run.in'), ('%s/%s' % (dirname, start_xyz), start_xyz)]
        if self.qmmm:
            # Send absolute path of qmindices and prmtop to the root folder of the WQ tmpdir
            in_files += [(self.qmindices_name, os.path.split(self.qmindices_name)[1]),
                         (self.prmtop_name, os.path.split(self.prmtop_name)[1])]
        out_files = [('%s/run.out' % dirname, 'run.out')]
        for f in guessfnms:
            in_files.append((os.path.join(dirname, f), f))
        out_scr = ['ca0', 'cb0'] if unrestricted else ['c0']
        out_scr += ['mullpop']
        for f in out_scr:
            out_files.append((os.path.join(dirname, self.scr, f), os.path.join(self.scr, f)))
        queue_up_src_dest(wq, "%s/runtc run.in &> run.out" % rootdir, in_files, out_files, verbose=False)

    def number_output(self, dirname, calcNum):
        if not os.path.exists(os.path.join(dirname, 'run.out')):
            raise RuntimeError('run.out does not exist')
        shutil.copy2(os.path.join(dirname,start_xyz), os.path.join(dirname,'start_%03i.%s' % (calcNum, os.path.splitext(start_xyz)[1])))
        shutil.copy2(os.path.join(dirname,'run.out'), os.path.join(dirname,'run_%03i.out' % calcNum))

    def read_wq_new(self, coords, dirname):
        # Extract energy and gradient
        try:
            # LPW note: Using python to call awk is not ideal and would take a bit of elbow grease to fix in the future.
            subprocess.call("awk '/FINAL ENERGY/ {p=$3} /Correlation Energy/ {p+=$5} /FINAL Target State Energy/ {p=$5} END {printf \"%.10f\\n\", p}' run.out > energy.txt", cwd=dirname, shell=True)
            subprocess.call("awk '/Gradient units are Hartree/,/Net gradient|Point charge part/ {if ($1 ~ /^-?[0-9]/) {print}}' run.out > grad.txt", cwd=dirname, shell=True)
            subprocess.call("awk 'BEGIN {s=0} /SPIN S-SQUARED/ {s=$3} END {printf \"%.6f\\n\", s}' run.out > s-squared.txt", cwd=dirname, shell=True)
            energy = float(open(os.path.join(dirname,'energy.txt')).readlines()[0].strip())
            na = len(self.qmindices) if self.qmmm else self.M.na
            gradient = np.loadtxt(os.path.join(dirname,'grad.txt'))[:na].flatten()
            s2 = float(open(os.path.join(dirname,'s-squared.txt')).readlines()[0].strip())
            assert gradient.shape[0] == self.M.na*3
        except (OSError, IOError, RuntimeError, subprocess.CalledProcessError):
            raise TeraChemEngineError
        return {'energy':energy, 'gradient':gradient, 's2':s2}

    def link_scratch(self, src, dest):
        if os.path.split(self.scr)[0]:
            raise RuntimeError("link_scratch cannot be used if self.scr is a nontrivial path")
        cwd = os.getcwd()
        destdirs = splitall(dest)
        if not os.path.exists(dest): os.makedirs(dest)
        if '..' in destdirs or '.' in destdirs: 
            raise RuntimeError('Destination path contains . or .. and is thus invalid')
        if not os.path.exists(os.path.join(src, self.scr)):
            logger.info(" TeraChem.link_scratch warning: %s/scr folder does not exist" % src)
        os.chdir(dest)
        pathlist = ['..']*len(splitall(dest)) + [src, self.scr]
        # print(pathlist)
        if os.path.exists(self.scr):
            if os.path.islink(self.scr): os.unlink(self.scr)
            else: shutil.rmtree(self.scr)
        os.symlink(os.path.join(*pathlist), self.scr)
        # print("Symlink created, check %s" % os.abspath(os.getcwd()))
        os.chdir(cwd)

class OpenMM(Engine):
    """
    Run a OpenMM energy and gradient calculation.
    """
    def __init__(self, molecule, pdb, xml):
        try:
            import simtk.openmm.app as app
            import simtk.openmm as mm
            import simtk.unit as u
        except ImportError:
            raise ImportError("OpenMM computation object requires the 'simtk' package. Please pip or conda install 'openmm' from omnia channel.")
        pdb = app.PDBFile(pdb)
        xmlSystem = False
        self.combination = None
        if os.path.exists(xml):
            xmlStr = open(xml).read()
            # check if we have opls combination rules if the xml is present
            try:
                self.combination = ET.fromstring(xmlStr).find('NonbondedForce').attrib['combination']
            except AttributeError:
                pass
            except KeyError:
                pass
            try:
                # If the user has provided an OpenMM system, we can use it directly
                system = mm.XmlSerializer.deserialize(xmlStr)
                xmlSystem = True
                logger.info("Treating the provided xml as a system XML file\n")
            except ValueError:
                logger.info("Treating the provided xml as a force field XML file\n")
        else:
            logger.info("xml file not in the current folder, treating as a force field XML file and setting up in gas phase.\n")
        if not xmlSystem:
            forcefield = app.ForceField(xml)
            system = forcefield.createSystem(pdb.topology, nonbondedMethod=app.NoCutoff, constraints=None, rigidWater=False)
        # apply opls combination rule if we are using it
        if self.combination == 'opls':
            logger.info("\nUsing geometric combination rules\n")
            system = self.opls(system)
        integrator = mm.VerletIntegrator(1.0*u.femtoseconds)
        platform = mm.Platform.getPlatformByName('Reference')
        self.simulation = app.Simulation(pdb.topology, system, integrator, platform)
        super(OpenMM, self).__init__(molecule)

    def calc_new(self, coords, dirname):
        from simtk.openmm import Vec3
        import simtk.unit as u
        try:
            self.M.xyzs[0] = coords.reshape(-1, 3) * bohr2ang
            pos = [Vec3(self.M.xyzs[0][i,0]/10, self.M.xyzs[0][i,1]/10, self.M.xyzs[0][i,2]/10) for i in range(self.M.na)]*u.nanometer
            self.simulation.context.setPositions(pos)
            state = self.simulation.context.getState(getEnergy=True, getForces=True)
            energy = state.getPotentialEnergy().value_in_unit(u.kilojoule_per_mole) / eqcgmx
            gradient = state.getForces(asNumpy=True).flatten() / fqcgmx
        except:
            raise OpenMMEngineError
        return {'energy':energy, 'gradient':gradient}

    @staticmethod
    def opls(system):
        """Apply the opls combination rule to the system."""

        from numpy import sqrt
        import simtk.openmm as mm

        # get system information from the openmm system
        forces = {system.getForce(index).__class__.__name__: system.getForce(index) for index in
                  range(system.getNumForces())}
        # use the nondonded_force tp get the same rules
        nonbonded_force = forces['NonbondedForce']
        lorentz = mm.CustomNonbondedForce(
            'epsilon*((sigma/r)^12-(sigma/r)^6); sigma=sqrt(sigma1*sigma2); epsilon=sqrt(epsilon1*epsilon2)*4.0')
        lorentz.setNonbondedMethod(mm.CustomNonbondedForce.NoCutoff)
        lorentz.addPerParticleParameter('sigma')
        lorentz.addPerParticleParameter('epsilon')
        lorentz.setCutoffDistance(nonbonded_force.getCutoffDistance())
        system.addForce(lorentz)
        ljset = {}
        # Now for each particle calculate the combination list again
        for index in range(nonbonded_force.getNumParticles()):
            charge, sigma, epsilon = nonbonded_force.getParticleParameters(index)
            # print(nonbonded_force.getParticleParameters(index))
            ljset[index] = (sigma, epsilon)
            lorentz.addParticle([sigma, epsilon])
            nonbonded_force.setParticleParameters(
                index, charge, 0, 0)
        for i in range(nonbonded_force.getNumExceptions()):
            (p1, p2, q, sig, eps) = nonbonded_force.getExceptionParameters(i)
            # ALL THE 12,13 interactions are EXCLUDED FROM CUSTOM NONBONDED FORCE
            # All 1,4 are scaled by the amount in the xml file
            lorentz.addExclusion(p1, p2)
            if eps._value != 0.0:
                # combine sigma using the geometric combination rule
                sig14 = sqrt(ljset[p1][0] * ljset[p2][0])
                nonbonded_force.setExceptionParameters(i, p1, p2, q, sig14, eps)
        return system

class Psi4(Engine):
    """
    Run a Psi4 energy and gradient calculation.
    """
    def __init__(self, molecule=None, threads=None):
        # molecule.py can not parse psi4 input yet, so we use self.load_psi4_input() as a walk around
        if molecule is None:
            # create a fake molecule
            molecule = Molecule()
            molecule.elem = ['H']
            molecule.xyzs = [[[0,0,0]]]
        super(Psi4, self).__init__(molecule)
        self.threads = threads

    def nt(self):
        if self.threads is not None:
            return " -n %i" % self.threads
        else:
            return ""

    def load_psi4_input(self, psi4in):
        """ Psi4 input file parser, only support xyz coordinates for now """
        coords = []
        elems = []
        fragn = []
        found_molecule, found_geo, found_gradient = False, False, False
        psi4_temp = [] # store a template of the input file for generating new ones
        for line in open(psi4in):
            if 'molecule' in line:
                found_molecule = True
                psi4_temp.append(line)
            elif found_molecule is True:
                ls = line.split()
                if len(ls) == 4:
                    if found_geo == False:
                        found_geo = True
                        psi4_temp.append("$!geometry@here")
                    # parse the xyz format
                    elems.append(ls[0])
                    coords.append(ls[1:4])
                elif '--' in line:
                    fragn.append(len(elems))
                else:
                    psi4_temp.append(line)
                    if '}' in line:
                        found_molecule = False
            else:
                psi4_temp.append(line)
            if "gradient(" in line:
                found_gradient = True
        if found_gradient == False:
            raise RuntimeError("Psi4 inputfile %s should have gradient() command." % psi4in)
        self.M = Molecule()
        self.M.elem = elems
        self.M.xyzs = [np.array(coords, dtype=np.float64)]
        self.psi4_temp = psi4_temp
        self.fragn = fragn

        print(self.M.elem)
        print(self.M.xyzs)

    def calc_new(self, coords, dirname):
        if not os.path.exists(dirname): os.makedirs(dirname)
        # Convert coordinates back to the xyz file
        self.M.xyzs[0] = coords.reshape(-1, 3) * bohr2ang
        # Write Psi4 input.dat
        with open(os.path.join(dirname, 'input.dat'), 'w') as outfile:
            for line in self.psi4_temp:
                if line == '$!geometry@here':
                    for i, (e, c) in enumerate(zip(self.M.elem, self.M.xyzs[0])):
                        if i in self.fragn:
                            outfile.write('--\n')
                        outfile.write("%-7s %13.7f %13.7f %13.7f\n" % (e, c[0], c[1], c[2]))
                else:
                    outfile.write(line)
        try:
            # Run Psi4
            subprocess.check_call('psi4%s input.dat' % self.nt(), cwd=dirname, shell=True)
            # Read energy and gradients from Psi4 output
            result = self.read_result(dirname)
        except (OSError, IOError, RuntimeError, subprocess.CalledProcessError):
            raise Psi4EngineError
        return result
    
    def read_result(self, dirname):
        """ Read Psi4 calculation output. """
        energy, gradient = None, None
        psi4out = os.path.join(dirname, 'output.dat')
        with open(psi4out) as outfile:
            found_grad = False
            found_num_grad = False
            for line in outfile:
                line_strip = line.strip()
                if line_strip.startswith('*'):
                    # this works for CCSD and CCSD(T) total energy
                    ls = line_strip.split()
                    if len(ls) > 4 and ls[2] == 'total' and ls[3] == 'energy':
                        energy = float(ls[-1])
                elif line_strip.startswith('Total Energy'):
                    # this works for DF-MP2 total energy
                    ls = line_strip.split()
                    if ls[-1] == '[Eh]':
                        energy = float(ls[-2])
                    else:
                        # this works for HF and DFT total energy
                        try:
                            energy = float(ls[-1])
                        except:
                            pass
                elif line_strip == '-Total Gradient:' or line_strip == '-Total gradient:':
                    # this works for most of the analytic gradients
                    found_grad = True
                    gradient = []
                elif found_grad is True:
                    ls = line_strip.split()
                    if len(ls) == 4:
                        if ls[0].isdigit():
                            gradient.append([float(g) for g in ls[1:4]])
                    else:
                        found_grad = False
                        found_num_grad = False
                elif line_strip == 'Gradient written.':
                    # this works for CCSD(T) gradients computed by numerical displacements
                    found_num_grad = True
                    logger.info("found num grad\n")
                elif found_num_grad is True and line_strip.startswith('------------------------------'):
                    for _ in range(4):
                        line = next(outfile)
                    found_grad = True
                    gradient = []
        if energy is None:
            raise RuntimeError("Psi4 energy is not found in %s, please check." % psi4out)
        if gradient is None:
            raise RuntimeError("Psi4 gradient is not found in %s, please check." % psi4out)
        gradient = np.array(gradient, dtype=np.float64).ravel()
        return {'energy':energy, 'gradient':gradient}

class QChem(Engine):
    def __init__(self, molecule, dirname=None, qcdir=None, threads=None):
        super(QChem, self).__init__(molecule)
        self.threads = threads
        self.prep_temp_folder(dirname, qcdir)

    def prep_temp_folder(self, dirname, qcdir):
        # Provide an initial qchem scratch folder (e.g. supplied initial guess)
        if not qcdir:
            self.qcdir = False
            return
        elif not os.path.exists(qcdir):
            raise QChemEngineError("qcdir points to a folder that doesn't exist")
        if not dirname:
            raise QChemEngineError("If qcdir is provided, dirname must also be provided")
        elif not os.path.exists(dirname):
            os.makedirs(dirname)
        shutil.copytree(qcdir, os.path.join(dirname, "run.d"))
        self.M.edit_qcrems({'scf_guess':'read'})
        self.qcdir = True

    def nt(self):
        if self.threads is not None:
            return " -nt %i" % self.threads
        else:
            return ""

    def calc_new(self, coords, dirname):
        if not os.path.exists(dirname): os.makedirs(dirname)
        # Convert coordinates back to the xyz file
        self.M.xyzs[0] = coords.reshape(-1, 3) * bohr2ang
        self.M.edit_qcrems({'jobtype':'force'})
        # LPW 2019-12-17: In some cases (such as running a FD Hessian calculation)
        # we may use multiple folders for running different calcs using the same engine.
        # When changing to a new folder without run.d, the calculation will crash if scf_guess is set to read.
        # So we check if run.d actually exists in the folder. If not, do not read the guess.
        # This is a hack and at some point we may want some more flexibility in which qcdir we use.
        if self.qcdir and not os.path.exists(os.path.join(dirname, 'run.d')):
            self.qcdir = False
            self.M.edit_qcrems({'scf_guess':None})
        self.M[0].write(os.path.join(dirname, 'run.in'))
        try:
            # Run Q-Chem
            if self.qcdir:
                subprocess.check_call('qchem%s -save run.in run.out run.d > run.log 2>&1' % self.nt(), cwd=dirname, shell=True)
            else:
                subprocess.check_call('qchem%s -save run.in run.out run.d > run.log 2>&1' % self.nt(), cwd=dirname, shell=True)
                # Assume reading the SCF guess is desirable
                self.qcdir = True
                self.M.edit_qcrems({'scf_guess':'read'})
            result = self.read_result(dirname)
        except (OSError, IOError, RuntimeError, subprocess.CalledProcessError):
            raise QChemEngineError
        return result

    def calc_wq_new(self, coords, dirname):
        wq = getWorkQueue()
        if not os.path.exists(dirname): os.makedirs(dirname)
        # Convert coordinates back to the xyz file<
        self.M.xyzs[0] = coords.reshape(-1, 3) * bohr2ang
        self.M.edit_qcrems({'jobtype':'force'})
        self.M[0].write(os.path.join(dirname, 'run.in'))
        in_files = [('%s/run.in' % dirname, 'run.in')]
        out_files = [('%s/run.out' % dirname, 'run.out'), ('%s/run.log' % dirname, 'run.log')]
        if self.qcdir:
            raise RuntimeError("--qcdir currently not supported with Work Queue")
        queue_up_src_dest(wq, "qchem%s run.in run.out &> run.log" % self.nt(), in_files, out_files, verbose=False)

    def number_output(self, dirname, calcNum):
        if not os.path.exists(os.path.join(dirname, 'run.out')):
            raise RuntimeError('run.out does not exist')
        shutil.copy2(os.path.join(dirname,'run.out'), os.path.join(dirname,'run_%03i.out' % calcNum))

    def read_result(self, dirname):
        M1 = Molecule('%s/run.out' % dirname)
        # In the case of multi-stage jobs, the last energy and gradient is what we want.
        energy = M1.qm_energies[-1]
        # gradient = M1.qm_grads[-1].flatten()
        # Parse gradient from GRAD file for improved precision.
        gradient = []
        gradmode = 0
        for line in open('%s/run.d/GRAD' % dirname).readlines():
            if line.strip().startswith('$gradient'):
                gradmode = 1
            elif line.startswith('$'):
                gradmode = 0
            elif gradmode:
                s = line.split()
                gradient.append([float(i) for i in s])
        gradient = np.array(gradient).flatten()
        # Assume that the last occurence of "S^2" is what we want.
        s2 = 0.0
        for line in open('%s/run.out' % dirname):
            if "<S^2>" in line:
                s2 = float(line.split()[-1])
        return {'energy':energy, 'gradient':gradient, 's2':s2}

class Gromacs(Engine):
    def __init__(self, molecule):
        super(Gromacs, self).__init__(molecule)

    def calc_new(self, coords, dirname):
        try:
            from forcebalance.gmxio import GMX
        except ImportError:
            raise ImportError("ForceBalance is needed to compute energies and gradients using Gromacs.")
        if not os.path.exists(dirname): os.makedirs(dirname)
        try:
            Gro = Molecule("conf.gro")
            Gro.xyzs[0] = coords.reshape(-1,3) * bohr2ang
            cwd = os.getcwd()
            shutil.copy2("topol.top", dirname)
            shutil.copy2("shot.mdp", dirname)
            os.chdir(dirname)
            Gro.write("coords.gro")
            G = GMX(coords="coords.gro", gmx_top="topol.top", gmx_mdp="shot.mdp")
            EF = G.energy_force()
            Energy = EF[0, 0] / eqcgmx
            Gradient = EF[0, 1:] / fqcgmx
            os.chdir(cwd)
        except (OSError, IOError, RuntimeError, subprocess.CalledProcessError):
            raise GromacsEngineError
        return {'energy':Energy, 'gradient':Gradient}


class Molpro(Engine):
    """
    Run a Molpro energy and gradient calculation.
    """
    def __init__(self, molecule=None, threads=None):
        # molecule.py can not parse molpro input yet, so we use self.load_molpro_input() as a walk around
        if molecule is None:
            # create a fake molecule
            molecule = Molecule()
            molecule.elem = ['H']
            molecule.xyzs = [[[0,0,0]]]
        super(Molpro, self).__init__(molecule)
        self.threads = threads
        self.molproExePath = None

    def molproExe(self):
        if self.molproExePath is not None:
            return self.molproExePath
        else:
            return "molpro"

    def set_molproexe(self, molproExePath):
        self.molproExePath = molproExePath

    def nt(self):
        if self.threads is not None:
            return " -n %i" % self.threads
        else:
            return ""

    def load_molpro_input(self, molproin):
        """ Molpro input file parser, only support xyz coordinates for now """
        coords = []
        elems = []
        labels = []
        found_molecule, found_geo, found_gradient = False, False, False
        molpro_temp = [] # store a template of the input file for generating new ones
        for line in open(molproin):
            if 'geometry' in line:
                found_molecule = True
                molpro_temp.append(line)
            elif found_molecule is True:
                ls = line.split()
                if len(ls) == 4:
                    if found_geo == False:
                        found_geo = True
                        molpro_temp.append("$!geometry@here")
                    # parse the xyz format
                    elem = re.search('[A-Z][a-z]*',ls[0]).group(0)
                    elems.append( elem ) # grabs the element
                    labels.append( ls[0].split(elem)[-1] ) # grabs label after element specification
                    coords.append(ls[1:4]) # grabs the coordinates
                else:
                    molpro_temp.append(line)
                    if '}' in line:
                        found_molecule = False
            else:
                molpro_temp.append(line)
            if "force" in line:
                found_gradient = True
        if found_gradient == False:
            raise RuntimeError("Molpro inputfile %s should have force command." % molproin)
        self.M = Molecule()
        self.M.elem = elems
        self.M.xyzs = [np.array(coords, dtype=np.float64)]
        self.labels = labels
        self.molpro_temp = molpro_temp

    def calc_new(self, coords, dirname):
        if not os.path.exists(dirname): os.makedirs(dirname)
        # Convert coordinates back to the xyz file
        self.M.xyzs[0] = coords.reshape(-1, 3) * bohr2ang
        # Write Molpro run.mol
        with open(os.path.join(dirname, 'run.mol'), 'w') as outfile:
            for line in self.molpro_temp:
                if line == '$!geometry@here':
                    for e, lab, c in zip(self.M.elem, self.labels, self.M.xyzs[0]):
                        outfile.write("%s%-7s %13.7f %13.7f %13.7f\n" % (e, lab, c[0], c[1], c[2]))
                else:
                    outfile.write(line)
        try:
            # Run Molpro
            subprocess.check_call('%s%s run.mol' % (self.molproExe(), self.nt()), cwd=dirname, shell=True)
            # Read energy and gradients from Molpro output
            result = self.read_result(dirname)
        except (OSError, IOError, RuntimeError, subprocess.CalledProcessError):
            raise MolproEngineError
        return result

    def number_output(self, dirname, calcNum):
        if not os.path.exists(os.path.join(dirname, 'run.out')):
            raise RuntimeError('run.out does not exist')
        shutil.copy2(os.path.join(dirname,'run.out'), os.path.join(dirname,'run_%03i.out' % calcNum))

    def read_result(self, dirname):
        """ read an output file from Molpro"""
        energy, gradient = None, None
        molpro_out = os.path.join(dirname, 'run.out')
        with open(molpro_out) as outfile:
            found_grad = False
            for line in outfile:
                line_strip = line.strip()
                fields = line_strip.split()
                if line_strip.startswith('!'):
                    # This works for RHF and RKS
                    if len(fields) == 5 and fields[-2] == 'Energy':
                        energy = float(fields[-1])
                    # This works for MP2, CCSD and CCSD(T) total energy
                    elif len(fields) == 4 and fields[1] == 'total' and fields[2] == 'energy:':
                        energy = float(fields[-1])
                elif len(fields) > 4 and fields[-4] == 'GRADIENT' and fields[-3] == 'FOR' and fields[-2] == 'STATE':
                    # this works for most of the analytic gradients
                    found_grad = True
                    gradient = []
                    # Skip three lines of header
                    next(outfile)
                    next(outfile)
                    next(outfile)
                elif found_grad is True:
                    if len(fields) == 4:
                        if fields[0].isdigit():
                            gradient.append([float(g) for g in fields[1:4]])
                    elif "Nuclear force contribution to virial" in line:
                        found_grad = False
                    else:
                        continue
        if energy is None:
            raise RuntimeError("Molpro energy is not found in %s, please check." % molpro_out)
        if gradient is None:
            raise RuntimeError("Molpro gradient is not found in %s, please check." % molpro_out)
        gradient = np.array(gradient, dtype=np.float64).ravel()
        return {'energy':energy, 'gradient':gradient}

class QCEngineAPI(Engine):
    def __init__(self, schema, program):
        try:
            import qcengine
        except ImportError:
            raise ImportError("QCEngine computation object requires the 'qcengine' package. Please pip or conda install 'qcengine'.")

        self.schema = schema
        self.program = program
        self.schema["driver"] = "gradient"

        self.M = Molecule()
        self.M.elem = list(schema["molecule"]["symbols"])

        # Geometry in (-1, 3) array in angstroms
        geom = np.array(schema["molecule"]["geometry"], dtype=np.float64).reshape(-1, 3) * bohr2ang
        self.M.xyzs = [geom]

        # Use or build connectivity
        if schema["molecule"].get("connectivity", None) is not None:
            self.M.Data["bonds"] = sorted((x[0], x[1]) for x in schema["molecule"]["connectivity"])
            self.M.built_bonds = True
        else:
            self.M.build_bonds()
        # one additional attribute to store each schema on the opt trajectory
        self.schema_traj = []

    def calc_new(self, coords, dirname):
        import qcengine
        new_schema = deepcopy(self.schema)
        new_schema["molecule"]["geometry"] = coords.tolist()
        new_schema.pop("program", None)
        ret = qcengine.compute(new_schema, self.program, return_dict=True)
        # store the schema_traj for run_json to pick up
        self.schema_traj.append(ret)
        if ret["success"] is False:
            raise QCEngineAPIEngineError("QCEngineAPI computation did not execute correctly. Message: " + ret["error"]["error_message"])
        # Unpack the energy and gradient
        energy = ret["properties"]["return_energy"]
        gradient = np.array(ret["return_result"])
        return {'energy':energy, 'gradient':gradient}

    def calc(self, coords, dirname):
        # overwrites the calc method of base class to skip caching and creating folders
        return self.calc_new(coords, dirname)

class ConicalIntersection(Engine):
    """
    Compute conical intersection objective function with penalty constraint.
    Implements the theory from Levine, Coe and Martinez, J. Phys. Chem. B 2008.
    """
    def __init__(self, molecule, engine1, engine2, sigma, alpha):
        self.engines = {1: engine1, 2: engine2}
        self.sigma = sigma
        self.alpha = alpha
        super(ConicalIntersection, self).__init__(molecule)

    def calc_new(self, coords, dirname):
        EDict = OrderedDict()
        GDict = OrderedDict()
        SDict = OrderedDict()
        for istate in [1, 2]:
            state_dnm = os.path.join(dirname, 'state_%i' % istate)
            if not os.path.exists(state_dnm): os.makedirs(state_dnm)
            try:
                spcalc = self.engines[istate].calc(coords, state_dnm)
            except EngineError:
                raise ConicalIntersectionEngineError
            EDict[istate] = spcalc['energy']
            GDict[istate] = spcalc['gradient']
            SDict[istate] = spcalc.get('s2', 0.0)
        # Determine the higher energy state
        if EDict[2] > EDict[1]:
            I = 2
            J = 1
        else:
            I = 1
            J = 2
        # Calculate energy and gradient avg and differences
        EAvg = 0.5*(EDict[I]+EDict[J])
        EDif = EDict[I]-EDict[J]
        GAvg = 0.5*(GDict[I]+GDict[J])
        GDif = GDict[I]-GDict[J]
        GAng = np.dot(GDict[I], GDict[J])/(np.linalg.norm(GDict[I])*np.linalg.norm(GDict[J]))
        # Compute penalty function
        Penalty = EDif**2 / (EDif + self.alpha)
        # Compute objective function and gradient
        Obj = EAvg + self.sigma * Penalty
        ObjGrad = GAvg + self.sigma * (EDif**2 + 2*self.alpha*EDif)/(EDif+self.alpha)**2 * GDif
        logger.info("EI= % .8f EJ= % .8f S2I= %.4f S2J= %.4f CosGrad= % .4f <E>= % .8f Gap= %.8f Pen= %.8f Obj= % .8f\n"
                    % (EDict[I], EDict[J], SDict[I], SDict[J], GAng, EAvg, EDif, Penalty, Obj))
        return {'energy':Obj, 'gradient':ObjGrad}

    def number_output(self, dirname, calcNum):
        for istate in [1, 2]:
            state_dnm = os.path.join(dirname, 'state_%i' % istate)
            self.engines[istate].number_output(state_dnm, calcNum)
