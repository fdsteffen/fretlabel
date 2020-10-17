#!/usr/bin/env python3

import numpy as np
import os
import argparse
import json
import glob
import multiprocessing
import relaxation
import tqdm
import tqdm.notebook
import itertools
import pickle
import copy

package_directory = os.path.dirname(os.path.abspath(__file__))

about = {}
with open(os.path.join(package_directory, '__about__.py')) as a:
    exec(a.read(), about)

_relax_types = ['D_p', 'D_ic', 'A_p', 'A_ic', 'q']


def parseCmd():
    """
    Read the command line arguments

    Returns
    -------
    directory : str
                path to the directory containing the .dat files (rkappa) and .xvg files(dye coordinates) 
    parameter_file : str
                     path to a json formatted parameter file for the burst, FRET and anisotropy calculations (see tutorial for an example)
    """ 
    parser = argparse.ArgumentParser(description='Compute FRET histograms from MD simulations with fluorophore labels')
    parser.add_argument('--version', action='version',
                        version='%(prog)s ' + str(about["__version__"]))
    parser.add_argument('-d', '--directory', 
                        help='Directory with gmx dyecouple output files', required=True)
    parser.add_argument('-p', '--parameters',
                        help='Parameter file (.json)', required=True)
    parser.add_argument('-o', '--output',
                        help='Output file o',
                        required=False)
    args = parser.parse_args()
    directory = args.directory
    parameter_file = args.parameters
    return directory, parameter_file


def readParameters(parameter_file):
    """
    Read parameters from a json file

    Parameters
    ----------
    parameter_file : str
                     path to a json formatted parameter file (see tutorial for an example)

    Returns
    -------
    parameters : dict
                 parameters for the burst, FRET and anisotropy calculations
    """
    with open(parameter_file, 'r') as f:
        parameters = json.load(f)
    return parameters


class Ensemble:
    """
    Instantiate a new ensemble of trajectories from different species

    Parameters
    ----------
    directory : str
                path to the directory containing the .dat files (rkappa) and .xvg files(dye coordinates) 
    parameters : dict
                 parameters for the burst, FRET and anisotropy calculations (see tutorial for an example)
    compute_anisotropy : bool
                         Calculate the time-resolved ensemble anisotropy. Requires a donor and acceptor .xvg file in the directory
                         where each contains xyz-coordinates of two atoms which define the transition dipole of the dye. 
    verbose : bool
              Output status and results to the command line
    """
    def __init__(self, directory, parameters, compute_anisotropy=False, verbose=True):
        self.species = []
        ps = parameters['species'] 
        for i in range(len(ps['name'])):
            filelist_rkappa = glob.glob(os.path.join(directory, ps['unix_pattern_rkappa'][i]))
            if not filelist_rkappa:
                raise IndexError('No rkappa files found for species \"{}\" with the pattern \"{}\" in the directory \"{}\".'.format(ps['name'][i], ps['unix_pattern_rkappa'][i], directory))
            
            if compute_anisotropy:
                filelist_don_coords = glob.glob(os.path.join(directory, ps['unix_pattern_don_coords'][i]))
                filelist_acc_coords = glob.glob(os.path.join(directory, ps['unix_pattern_acc_coords'][i]))
                if (not filelist_don_coords) or (not filelist_acc_coords):
                    raise NameError('No dye coordinates files found for species \"{}\" with the pattern \"{}\" or \"{}\" in the directory \"{}\".'.format(ps['name'][i], 
                        ps['unix_pattern_don_coords'][i], ps['unix_pattern_acc_coords'][i], directory))
                elif (len(filelist_rkappa) != len(filelist_don_coords)) or (len(filelist_rkappa) != len(filelist_acc_coords)):
                    raise ValueError('There are not the same number of rkappa and dye coordinates files in the directory \"{}\".'.format(directory))
                else:
                    self.species.append(Species(ps['name'][i], ps['probability'][i], filelist_rkappa, filelist_don_coords, filelist_acc_coords))
            else:
                self.species.append(Species(ps['name'][i], ps['probability'][i], filelist_rkappa))

        self.checkTimeStepIdentity()


    def checkTimeStepIdentity(self):
        """
        Verify that the trajectories of all species in the ensemble have the same time step
        """
        dt = [traj.dt for species in self.species for traj in species.trajectories]
        if len(dt)>1 and (not dt[1:] == dt[:-1]):
            raise ValueError('Timestep of trajectories is not the same across the ensemble.')
        else:
            self.dt = dt[0]


class Species:
    """
    Create a new species of trajectories

    Parameters
    ----------
    name : str
           name of the species
    probability : float
                  relative weight of the species in the ensemble
    filelist_rkappa : list of str
                      list of .dat filenames with inter-dye distance (R) and kappasquare values
    filelist_don_coords : list of str (optional)
                          list of .xvg filenames with xyz coordinates of two atoms defining the transition dipole of the donor dye
    filelist_acc_coords : list of str (optional)
                          list of .xvg filenames with xyz coordinates of two atoms defining the transition dipole of the acceptor dye
    """
    def __init__(self, name, probability, filelist_rkappa, filelist_don_coords=None, filelist_acc_coords=None):
        self.name = name
        self.probability = probability
        self.trajectories = []
        total_frames = 0
        for i, rkappa_filename in enumerate(filelist_rkappa):
            try:
                traj = Trajectory.from_file(rkappa_filename, filelist_don_coords[i], filelist_acc_coords[i])
            except TypeError:
                traj = Trajectory.from_file(rkappa_filename)
            self.trajectories.append(traj)
            total_frames += len(traj.time)
        for traj in self.trajectories:
            setattr(traj, 'weight', len(traj.time)/total_frames)


class Trajectory:
    """
    Time trajectory (in ps) of inter-dye distances (in nm) and kappa-square values

    Parameters
    ----------
    time : numpy.array
           time in picoseconds
    R : numpy.array
        inter-dye distance in nanometers
    kappasquare : numpy.array
                  kappasquare values calculated from the orientation of the donor and acceptor transition dipoles
    donor_xyz : numpy.ndarray (optional)
                xyz coordinates of two atoms defining the transition dipole of the donor dye
    acceptor_xyz : numpy.ndarray (optional)
                   xyz coordinates of two atoms defining the transition dipole of the acceptor dye
    """
    def __init__(self, time, R, kappasquare, donor_xyz=None, acceptor_xyz=None):
        self.time = time
        self.R = R
        self.kappasquare = kappasquare
        self.length = len(time)
        self.dt = (time[1]-time[0])/1000 # in nanoseconds
        self.k_fret = None
        self.p_fret = None
        self.pD_totfret = None
        self.weight = None
        if (donor_xyz is not None) and (acceptor_xyz is not None):
            self.checkLengthIdentity(self.length, donor_xyz, acceptor_xyz)
        self.donorTD = self.transitionDipole(donor_xyz)
        self.acceptorTD = self.transitionDipole(acceptor_xyz)


    @classmethod
    def from_file(cls, rkappa_filename, don_coords_filename=None, acc_coords_filename=None):
        """
        Create a fluordynamics.fluorburst.Trajectory class from filenames

        Parameters
        ----------
        rkappa_filename : str
                          name of a .dat file containing inter-dye distances and kappasquare values
        don_coords_filename : str (optional)
                              name of a .xvg file containing xyz coordinates of two atoms defining the transition dipole of the donor dye
        acc_coords_filename : str (optional)
                              name of a .xvg file containing xyz coordinates of two atoms defining the transition dipole of the acceptor dye
        """
        rkappa = np.loadtxt(rkappa_filename)
        if don_coords_filename is not None:
            donor_xyz = np.loadtxt(don_coords_filename, comments=['@', '#'])
        else:
            donor_xyz = None
        if acc_coords_filename is not None:
            acceptor_xyz = np.loadtxt(acc_coords_filename, comments=['@', '#'])
        else:
            acceptor_xyz = None
        return cls(rkappa[:,0], rkappa[:,1], rkappa[:,2], donor_xyz, acceptor_xyz)


    def checkLengthIdentity(self, traj_length, donor_xyz, acceptor_xyz):
        all_lengths = [traj_length, len(donor_xyz[:,0]), len(acceptor_xyz[:,0])]
        if all_lengths[1:] != all_lengths[:-1]:
            raise ValueError('Length of rkappa and dye coordinates is not the same [{}].'.format(', '.join(all_lengths)))


    @staticmethod
    def transitionDipole(dye_xyz):
        """
        Compute a normalized transition dipole vector

        Parameters
        ----------
        dye_xyz : numpy.ndarray
                  array of shape [n,7] with columns: time, x1, y1, z1, x2, y2, z2 where 1 and 2 are two atoms defining the transition dipole
        """
        try:
            TD = dye_xyz[:,4:7]-dye_xyz[:,1:4]
        except IndexError:
            print('Dye coordinate file has a wrong format. Rerun \"gmx traj\" with an index file containing two atoms which define the transition dipole.')
            return None
        except TypeError:
            return None
        else:
            return TD/np.linalg.norm(TD, axis=1, keepdims=True)


class Burst:
    """
    Create a new burst

    Parameters
    ----------
    burstsize : int
                total number of photons (donor + acceptor) to generate in this burst
    QD : float
         donor fluorescence quantum yield
    QA : float
         acceptor fluorescence quantum yield
    """
    def __init__(self, burstsize, QD, QA):
        self.burstsize = burstsize
        self.QY_correction = True
        self.gamma_correction = True
        self.events_DD_DA = {t: 0 for t in _relax_types}
        self.events_AA = {t: 0 for t in _relax_types}
        self.decaytimes_DD_DA = {t: [] for t in _relax_types}
        self.decaytimes_AA = {t: [] for t in _relax_types}
        self.polarizations = {t: [] for t in ['D_p', 'A_p']}
        self.FRETefficiency = None


    def checkBurstSizeReached(self, QD, QA, QY_correction):
        """
        Check if the number of photons has reached the specified burstsize
        
        Parameters
        ----------
        QD : float
             donor fluorescence quantum yield
        QA : float
             acceptor fluorescence quantum yield
        QY_correction : bool
                        correct the number of donor and acceptor photons by their respective quantum yield when evaluating whether the burstsize is reached.
                        e.g. say we collected 20 donor photons (with QD=0.5) and 25 acceptor photons (with QA=0.75) and the burstsize is set to be 50.
                        If QY_correction = False the total photon count is 20+25=45 photons, so the required burstsize is not reached yet.
                        if QY_correction = True the total photon count is (20/0.5 + 25/0.75 = 73) and so the burst is complete.
        """
        self.QY_correction = QY_correction
        if QY_correction and (self.events_DD_DA['D_p']/QD + self.events_DD_DA['A_p']/QA >= self.burstsize):
            return True
        elif (not QY_correction) and (self.events_DD_DA['D_p'] + self.events_DD_DA['A_p'] == self.burstsize):
            return True


    def addRelaxationEvent(self, event, decaytime, polarization, AA):
        """
        Classify the relaxation event

        Parameters
        ----------
        event : int
                type of relaxation event (1: donor photon, -1: internal conversion from donor, 
                2: acceptor photon, -2: internal conversion from acceptor, 0: relaxation due to inter-dye quenching)
        decaytime : float
                    time in nanoseconds after the excitation event
        polarization : int
                       polarization of the donor or acceptor photon (0: p, parallel, 1: s, orthogonal)
        AA : bool
             True = relaxation event after acceptor excitation (as in an ns-ALEX/PIE experiment)
             False = relaxation event after donor excitation
        """
        if AA:
            if event == 2:
                self.events_AA['A_p'] += 1
                self.decaytimes_AA['A_p'].append(decaytime)
                self.polarizations['A_p'].append(polarization)
            elif event == -2:
                self.events_AA['A_ic'] += 1
                self.decaytimes_AA['A_ic'].append(decaytime)
        else:
            if event == 1:
                self.events_DD_DA['D_p'] += 1
                self.decaytimes_DD_DA['D_p'].append(decaytime)
                self.polarizations['D_p'].append(polarization)
            elif event == -1:
                self.events_DD_DA['D_ic'] += 1
                self.decaytimes_DD_DA['D_ic'].append(decaytime)
            elif event == 2:
                self.events_DD_DA['A_p'] += 1
                self.decaytimes_DD_DA['A_p'].append(decaytime)
            elif event == -2:
                self.events_DD_DA['A_ic'] += 1
                self.decaytimes_DD_DA['A_ic'].append(decaytime)
            else:
                self.events_DD_DA['q'] += 1
                self.decaytimes_DD_DA['q'].append(decaytime)


    def calcFRET(self, no_gamma, QD, QA):
        """
        Calculate the transfer efficiency based on the donor and acceptor photon counts upon donor excitation

        Parameters
        ----------
        no_gamma : bool
                   mimic an uncorrected FRET experiment (i.e. before gamma-correction) which is affected by the different 
                   quantum yields of donor and acceptor (note: the detection efficiency ratio always set to be 1).
                   If the simulation should be comapred to a gamma-corrected experiment this parameter should be set to False.
        QD : float
             donor fluorescence quantum yield
        QA : float
             acceptor fluorescence quantum yield
        """
        self.no_gamma = no_gamma
        if no_gamma:
            self.FRETefficiency = (self.events_DD_DA['A_p']/QA) / (self.events_DD_DA['A_p']/QA+ self.events_DD_DA['D_p']/QD)
        else:
            self.FRETefficiency = self.events_DD_DA['A_p'] / (self.events_DD_DA['A_p']+ self.events_DD_DA['D_p'])


class Relaxation:
    """
    Create a relaxation event (photon emission or internal conversion)

    Parameters
    ----------
    trajectory : fluordynamics.burst.Trajectory
                 trajectory object containing inter-dye distances, kappasquare values and donor/acceptor dye transition dipoles
    pD_tot : float
             probability of a donor relaxation event (photon emission + internal conversion)
    pA_tot : float
             probability of a acceptor relaxation event (photon emission + internal conversion)
    quenching_radius : float
                       minimum distance beyond which inter-dye quenching will lead to a non-radiative decay (e.g. Dexter energy transfer)
    skipframesatstart : int
                        number of frames to skip at the beginning of the trajectory
    skipframesatend : int
                        number of frames to skip at the end of the trajectory
    QD : float
         donor fluorescence quantum yield
    QA : float
         acceptor fluorescence quantum yield
    compute_anisotropy : bool
                         Calculate the time-resolved ensemble anisotropy. Requires a donor and acceptor transition dipole to be present in the trajectory. 
    """
    def __init__(self, trajectory, pD_tot, pA_tot, quenching_radius, skipframesatstart, skipframesatend, QD, QA, compute_anisotropy):
        event_DD_DA = 0
        while event_DD_DA == 0:
            self.excitation_ndx_DD_DA = relaxation.findExcitationIndex(trajectory.length, skipframesatstart, skipframesatend)
            if (self.excitation_ndx_DD_DA < 0) or (self.excitation_ndx_DD_DA > trajectory.length):
                raise ValueError('The excitation index lies outside of the trajectory. Adjust the skipframesatstart and skipframesatend parameters.')
            event_DD_DA, self.relaxation_ndx_DD_DA = relaxation.findRelaxationIndex_DD_DA(pD_tot, trajectory.pD_totfret, self.excitation_ndx_DD_DA, trajectory.length) # relaxation upon donor excitation (D emission/IC or FRET)
            
        event_AA = 0
        while event_AA == 0:
            self.excitation_ndx_AA = relaxation.findExcitationIndex(trajectory.length, skipframesatstart, skipframesatend)
            if (self.excitation_ndx_AA < 0) or (self.excitation_ndx_AA > trajectory.length):
                raise ValueError('The excitation index lies outside of the trajectory. Adjust the skipframesatstart and skipframesatend parameters.')
            event_AA, self.relaxation_ndx_AA = relaxation.findRelaxationIndex_AA(pA_tot, self.excitation_ndx_AA, trajectory.length) # relaxation upon acceptor excitation (A emission/IC)
        
        self.event_DD_DA = relaxation.checkRelaxationIndex(event_DD_DA, self.excitation_ndx_DD_DA, self.relaxation_ndx_DD_DA, trajectory.R, quenching_radius, QD, QA)
        self.event_AA = relaxation.checkRelaxationIndex(event_AA, self.excitation_ndx_AA, self.relaxation_ndx_AA, trajectory.R, quenching_radius, QD, QA)
        
        if compute_anisotropy and (self.event_DD_DA == 1):
            self.polarization_DD = relaxation.polarization(trajectory.donorTD[self.excitation_ndx_DD_DA,:], trajectory.donorTD[self.relaxation_ndx_DD_DA,:]) 
        else:
            self.polarization_DD = None
        if compute_anisotropy and (self.event_AA == 2):
            self.polarization_AA = relaxation.polarization(trajectory.acceptorTD[self.excitation_ndx_AA,:], trajectory.acceptorTD[self.relaxation_ndx_AA,:])
        else:
            self.polarization_AA = None

        self.decaytime_DD_DA = (self.relaxation_ndx_DD_DA - self.excitation_ndx_DD_DA) * trajectory.dt
        self.decaytime_AA = (self.relaxation_ndx_AA - self.excitation_ndx_AA) * trajectory.dt


class Experiment:
    """
    Setup an in silico fluorescence experiment

    Parameters
    ----------
    directory : str
                path to the directory containing the .dat files (rkappa) and .xvg files(dye coordinates)
    parameters : dict
                 parameters for the burst, FRET and anisotropy calculations (see tutorial for an example)
    binwidth : float
               time between photon bins
    compute_anisotropy : bool
                         Calculate the time-resolved ensemble anisotropy. Requires a donor and acceptor .xvg file in the directory
                         where each contains xyz-coordinates of two atoms which define the transition dipole of the dye.
    verbose : bool
              Output status and results to the command line
    show_progress : bool
                    Display the progress of the burst calculation as a status bar
    """
    def __init__(self, directory, parameters, binwidth=0.025, compute_anisotropy=False, verbose=True, show_progress=True):
        self.parameters = parameters
        self.parameters['fret']['R0_const'] = parameters['fret']['R0'] / parameters['fret']['kappasquare']**(1/6)
        self.compute_anisotropy = compute_anisotropy
        self.ensemble = Ensemble(directory, parameters, compute_anisotropy, verbose)
        self.calcTransitionRates()

        if verbose:
            self.print_settings(compute_anisotropy)

        for i, species in enumerate(self.ensemble.species):
            for j, traj in enumerate(species.trajectories):
                self.ensemble.species[i].trajectories[j].k_fret = self.calcTransferRate(traj.R)
                self.ensemble.species[i].trajectories[j].p_fret = self.ensemble.species[i].trajectories[j].k_fret * self.ensemble.dt
                self.ensemble.species[i].trajectories[j].pD_totfret = self.ensemble.species[i].trajectories[j].p_fret + self.transProb['pD_tot']
        
        if self.parameters['sampling']['multiprocessing']:
            burstsizes = self.calcBurstsizes()
            if show_progress:
                with multiprocessing.Pool() as pool:
                    if os.environ['_'].split('/')[-1] == 'jupyter':
                        self.bursts = list(tqdm.notebook.tqdm(pool.imap(self.calcBurst, burstsizes, chunksize=int(self.parameters['sampling']['nbursts']/50)), total = self.parameters['sampling']['nbursts'], desc='Calculating bursts', bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{remaining} s]"))
                    else:
                        self.bursts = list(tqdm.tqdm(pool.imap(self.calcBurst, burstsizes, chunksize=int(self.parameters['sampling']['nbursts']/50)), total = self.parameters['sampling']['nbursts'], desc='Calculating bursts', bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{remaining} s]"))
            else: 
                self.bursts = pool.map(self.calcBurst, burstsizes)
        else:
            n = self.parameters['sampling']['nbursts']
            burstsizes = self.calcBurstsizes()
            if show_progress:
                if os.environ['_'].split('/')[-1] == 'jupyter':
                    pbar = tqdm.notebook.tqdm(total = n, desc='Calculating bursts', bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{remaining} s]")
                else:
                    pbar = tqdm.tqdm(total = n, desc='Calculating bursts', bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{remaining} s]")
            self.bursts = []
            for i, bs in enumerate(burstsizes):
                self.bursts.append(self.calcBurst(bs))
                if show_progress:
                    pbar.update()                          

        self.FRETefficiencies = np.array([burst.FRETefficiency for burst in self.bursts])
        self.burstsizes = np.array([burst.burstsize for burst in self.bursts])
        self.decaytimes_DD_DA = {}
        self.decaytimes_AA = {}
        self.polarizations = {}
        self.polIntensity = {}
        self.anisotropy = {}
        for t in _relax_types:
            self.decaytimes_DD_DA[t] = np.array([decaytime for decaytimes in [burst.decaytimes_DD_DA[t] for burst in self.bursts] for decaytime in decaytimes])
            self.decaytimes_AA[t] = np.array([decaytime for decaytimes in [burst.decaytimes_AA[t] for burst in self.bursts] for decaytime in decaytimes])
        if compute_anisotropy:
            for t in ['D_p', 'A_p']:
                self.polarizations[t] = np.array([pol for polarizations in [burst.polarizations[t] for burst in self.bursts] for pol in polarizations])
                if t == 'D_p':
                    self.polIntensity[t] = self.polarizationIntensity(binwidth, self.decaytimes_DD_DA[t], self.polarizations[t])
                else:
                    self.polIntensity[t] = self.polarizationIntensity(binwidth, self.decaytimes_AA[t], self.polarizations[t])
                self.anisotropy[t] = self.calcAnisotropy(self.polIntensity[t])

        if verbose:
            self.print_results()


    @staticmethod
    def polarizationIntensity(binwidth, decaytimes, polarizations):
        """
        Calculate the polarization-resolved fluorescence intensities

        Parameters
        ----------
        binwidth : float
                   time between photon bins
        decaytimes : dict of numpy.ndarray
                     Decaytimes for the different relaxation types collected from all bursts
        polarizations : dict of numpy.ndarray
                        Photon polarization for donor photons after donor excitation and acceptor photons after acceptor excitation
                        The encoding is 0 = parallel (p) and 1 = orthogonal (s) 

        Returns
        -------
        polIntensity : numpy.ndarray
                       polarization intensity array of shape [n,3] with columns: time bins, p-photons counts (parallel), s-photon counts (orthogonal)
        """
        time_pol = sorted(zip(decaytimes, polarizations))
        polIntensity = [[], [], []]
        for key, group in itertools.groupby(time_pol, lambda tp: int(tp[0] // binwidth)+1):
            l = list([tp[1] for tp in group])
            s = np.count_nonzero(l)
            polIntensity[2].append(s)
            polIntensity[1].append(len(l)-s)
            polIntensity[0].append(key*binwidth)
        return np.array(polIntensity).T


    @staticmethod
    def calcAnisotropy(polIntensity):
        """
        Calculate the time-resolved anisotropy decay

        Parameters
        ----------
        polIntensity : numpy.ndarray
                       polarization intensity array of shape [n,3] with columns: time bins, p-photons counts (parallel), s-photon counts (orthogonal)
        
        Returns
        -------
        anisotropy : numpy.ndarray
                     anisotropy array of shape [n,2] with columns: time bins, anisotropy
        """
        r = 2/5 * (polIntensity[:,1]-polIntensity[:,2]) / (polIntensity[:,1]+2*polIntensity[:,2])
        ansiotropy = np.vstack((polIntensity[:,0], r)).T
        return ansiotropy
         

    def calcTransitionRates(self):
        """
        Calculate the time-independent transition rates and probabilities
        """
        self.rates = {'kD_f': self.parameters['dyes']['QD'] / self.parameters['dyes']['tauD'],
                      'kA_f': self.parameters['dyes']['QA'] / self.parameters['dyes']['tauA'], 
                      'kD_ic': (1-self.parameters['dyes']['QD']) / self.parameters['dyes']['tauD'], 
                      'kA_ic': (1-self.parameters['dyes']['QA']) / self.parameters['dyes']['tauA']}
        self.rates['kD_tot'] = self.rates["kD_f"] + self.rates["kD_ic"]
        self.rates['kA_tot'] = self.rates["kA_f"] + self.rates["kA_ic"]

        self.transProb = {'pD_f': self.rates['kD_f'] * self.ensemble.dt,
                          'pA_f': self.rates['kA_f'] * self.ensemble.dt,
                          'pD_ic': self.rates['kD_ic'] * self.ensemble.dt,
                          'pA_ic': self.rates['kA_ic'] * self.ensemble.dt,
                          'pD_tot': self.rates['kD_tot'] * self.ensemble.dt,
                          'pA_tot': self.rates['kA_tot'] * self.ensemble.dt}


    def calcTransferRate(self, R):
        """
        Calculated the time-dependent transfer rate

        Parameters
        ----------
        R : numpy.ndarray
            inter-dye distances

        Returns
        -------
        k_fret : numpy.ndarray
                 Rates of transfer efficiency dependent on the inter-dye distance, the Förster radius and the relative dye orientation (kappasquare)
        """
        k_fret = self.rates['kD_tot'] * self.parameters['fret']['kappasquare'] * (self.parameters['fret']['R0_const'] / R)**6
        return k_fret


    def calcBurst(self, burstsize):
        """
        Compute a photon burst

        Parameters
        ---------- 
        burstsize : int
                    total number of photons (donor + acceptor) to generate in this burst

        Returns
        -------
        burst : fluordynamics.fluorburst.Burst
                Burst object containing a series of relaxation events with associated decaytimes, polarizations and FRET efficiencies
        """
        burst = Burst(burstsize, self.parameters['dyes']['QD'], self.parameters['dyes']['QA'])
        species_probs = [species.probability for species in self.ensemble.species]
        traj_weights = {species.name: [traj.weight for traj in species.trajectories] for species in self.ensemble.species}

        if self.parameters['bursts']['averaging'].lower() != 'ensemble':
            species = self.ensemble.species[relaxation.integerChoice(species_probs)]
        if (self.parameters['bursts']['averaging'].lower() != 'ensemble') and (self.parameters['bursts']['averaging'].lower() != 'species'):
            trajectory = species.trajectories[relaxation.integerChoice(traj_weights[species.name])]
            self.parameters['bursts']['averaging'] = 'trajectory'
        
        while True:
            if self.parameters['bursts']['averaging'].lower() == 'ensemble':
                species = self.ensemble.species[relaxation.integerChoice(species_probs)]
            if (self.parameters['bursts']['averaging'].lower() == 'ensemble') or (self.parameters['bursts']['averaging'].lower() =='species'):
                trajectory = species.trajectories[relaxation.integerChoice(traj_weights[species.name])]
            relax = Relaxation(trajectory, self.transProb['pD_tot'], self.transProb['pA_tot'], self.parameters['fret']['quenching_radius'], 
                               self.parameters['sampling']['skipframesatstart'], self.parameters['sampling']['skipframesatend'],
                               self.parameters['dyes']['QD'], self.parameters['dyes']['QA'], self.compute_anisotropy)
            burst.addRelaxationEvent(relax.event_DD_DA, relax.decaytime_DD_DA, relax.polarization_DD, False)
            burst.addRelaxationEvent(relax.event_AA, relax.decaytime_AA, relax.polarization_AA, True)
            if burst.checkBurstSizeReached(self.parameters['dyes']['QD'], self.parameters['dyes']['QA'], self.parameters['bursts']['QY_correction']):
                break
        burst.calcFRET(self.parameters['fret']['no_gamma'], self.parameters['dyes']['QD'], self.parameters['dyes']['QA'])
        return burst


    def calcBurstsizes(self):
        """
        Compute random burstsizes x_1,x_2,...x_n based on an analytical burstsize distribution P(x) = x^lambda 

        Returns
        -------
        burstsizes : numpy.ndarray
                     Array of burstsizes within a range defined by a lower and upper threshold
        """
        burstsizetable = np.arange(self.parameters['bursts']["lower_limit"], self.parameters['bursts']["upper_limit"]+1)
        bcounts = burstsizetable**(self.parameters['bursts']["lambda"])
        rng = np.random.default_rng()
        burstsizes = rng.choice(a=burstsizetable, size=self.parameters['sampling']["nbursts"], p=bcounts/sum(bcounts))
        return burstsizes


    def save(self, filename, remove_bursts=False):
        """
        Pickle the experiment class to a file

        Parameters
        ----------
        filename : str
        """
        with open(filename, 'wb') as file:
            if remove_bursts:
                exp_noburst = copy.copy(self)
                exp_noburst.bursts = None
                pickle.dump(exp_noburst, file)
            else:
                pickle.dump(self, file)

    @classmethod
    def load(cls, filename):
        """
        Load an instance of the experiment class from a pickle file

        Parameters
        ----------
        filename : str
        """
        with open(filename, 'rb') as file:
            return pickle.load(file)
           

    def print_settings(self, compute_anisotropy):
        """
        Print the rates and settings of the in-silico experiment

        Parameters
        ----------
        """
        print('\n------------------------------------\nFluordynamics {} - FRET in silico\n------------------------------------\n'.format(about['__version__']))
        print('Orientation independent R0_const = {:0.2f} nm'.format(self.parameters['fret']['R0_const']))
        
        print('''
              donor    acceptor
QY            {:0.2f}    {:0.2f} 
tau (ns)      {:0.2f}    {:0.2f}
k_f (ns^-1)   {:0.2f}    {:0.2f}
k_ic (ns^-1)  {:0.2f}    {:0.2f}
              '''.format(self.parameters['dyes']['QD'], self.parameters['dyes']['QA'],
                         self.parameters['dyes']['tauD'], self.parameters['dyes']['tauA'],
                         self.rates['kD_f'], self.rates['kA_f'],
                         self.rates['kD_ic'], self.rates['kA_ic']))

        print('Burst averaging method: {}'.format(self.parameters['bursts']['averaging']))

        if compute_anisotropy:
            print('Calculate anisotropy: yes\n')
        else:
            print('Calculate anisotropy: no\n')

    def print_results(self):
        print('\n\naverage FRET efficiency: {:0.2f} +- {:0.2f}\n'.format(np.mean(self.FRETefficiencies), np.std(self.FRETefficiencies)))
        print('------------\nHow to cite:\n------------\n\"An atomistic view on carbocyanine photophysics in the realm of RNA\"\nF.D. Steffen, R.K.O. Sigel, R. Börner, Phys. Chem. Chem. Phys (2016)\n\n\n'
'This project was inspired by md2fret:\n\n\"In silico FRET from simulated dye dynamics\"\nM. Hoefling, H. Grubmüller, Comp. Phys. Commun. (2013)\n')


if __name__ == "__main__":
    directory, parameter_file = parseCmd()
    parameters = readParameters(parameter_file)
    experiment = Experiment(directory, parameters, binwidth=0.025, compute_anisotropy=False, verbose=True)

