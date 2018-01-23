"""
Contact map analysis.
"""
# Maintainer: David W.H. Swenson (dwhs@hyperblazer.net)
# Licensed under LGPL, version 2.1 or greater
import collections
import itertools
import pickle
import json
import numpy as np
import pandas as pd
import mdtraj as md

from .contact_count import ContactCount
from .py_2_3 import inspect_method_arguments

# TODO:
# * switch to something where you can define the haystack -- the trick is to
#   replace the current mdtraj._compute_neighbors with something that
#   build a voxel list for the haystack, and then checks the voxel for each
#   query atom. Doesn't look like anything is doing that now: neighbors
#   doesn't use voxels, neighborlist doesn't limit the haystack

def residue_neighborhood(residue, n=1):
    """Find n nearest neighbor residues

    Parameters
    ----------
    residue : mdtraj.Residue
        this residue
    n : positive int
        number of neighbors to find

    Returns
    -------
    list of int
        neighbor residue numbers
    """
    neighborhood = set([residue.index+i for i in range(-n, n+1)])
    chain = set([res.index for res in residue.chain.residues])
    # we could probably choose an faster approach here, but this is pretty
    # good, and it only gets run once per residue
    return [idx for idx in neighborhood if idx in chain]

def _residue_and_index(residue, topology):
    res = residue
    try:
        res_idx = res.index
    except AttributeError:
        res_idx = residue
        res = topology.residue(res_idx)
    return (res, res_idx)


class ContactObject(object):
    """
    Generic object for contact map related analysis. Effectively abstract.

    Much of what we need to do the contact map analysis is the same for all
    analyses. It's in here.
    """
    def __init__(self, topology, query, haystack, cutoff, n_neighbors_ignored):
        # all inits required: no defaults for abstract class!
        self._topology = topology
        if query is None:
            query = topology.select("not water and symbol != 'H'")
        if haystack is None:
            haystack = topology.select("not water and symbol != 'H'")
        # make things private and accessible through read-only properties so
        # they don't get accidentally changed after analysis
        self._cutoff = cutoff
        self._query = set(query)
        self._haystack = set(haystack)
        self._n_neighbors_ignored = n_neighbors_ignored
        self._atom_idx_to_residue_idx = {atom.index: atom.residue.index
                                         for atom in self.topology.atoms}

    def __hash__(self):
        return hash((self.cutoff, self.n_neighbors_ignored,
                     frozenset(self._query), frozenset(self._haystack),
                     self.topology))

    def __eq__(self, other):
        is_equal = (self.cutoff == other.cutoff
                    and self.n_neighbors_ignored == other.n_neighbors_ignored
                    and self.query == other.query
                    and self.haystack == other.haystack
                    and self.topology == other.topology)
        return is_equal

    def to_dict(self):
        """Convert object to a dict.

        Keys should be strings; values should be (JSON-) serializable.

        See also
        --------
        from_dict
        """
        # need to explicitly convert possible np.int64 to int in several
        dct = {
            'topology': self._serialize_topology(self.topology),
            'cutoff': self._cutoff,
            'query': list([int(val) for val in self._query]),
            'haystack': list([int(val) for val in self._haystack]),
            'n_neighbors_ignored': self._n_neighbors_ignored,
            'atom_idx_to_residue_idx': self._atom_idx_to_residue_idx,
            'atom_contacts': \
                self._serialize_contact_counter(self._atom_contacts),
            'residue_contacts': \
                self._serialize_contact_counter(self._residue_contacts)
        }
        return dct

    @classmethod
    def from_dict(cls, dct):
        """Create object from dict.

        Parameters
        ----------
        dct : dict
            dict-formatted serialization (see to_dict for details)

        See also
        --------
        to_dict
        """
        deserialize_set = set
        deserialize_atom_to_residue_dct = lambda d: {int(k): d[k] for k in d}
        deserialization_helpers = {
            'topology': cls._deserialize_topology,
            'atom_contacts': cls._deserialize_contact_counter,
            'residue_contacts': cls._deserialize_contact_counter,
            'query': deserialize_set,
            'haystack': deserialize_set,
            'atom_idx_to_residue_idx': deserialize_atom_to_residue_dct
        }
        for key in deserialization_helpers:
            if key in dct:
                dct[key] = deserialization_helpers[key](dct[key])

        kwarg_keys = inspect_method_arguments(cls.__init__)
        set_keys = set(dct.keys())
        missing = set(kwarg_keys) - set_keys
        dct.update({k: None for k in missing})
        instance = cls.__new__(cls)
        for k in dct:
            setattr(instance, "_" + k, dct[k])
        return instance

    @staticmethod
    def _deserialize_topology(topology_json):
        """Create MDTraj topology from JSON-serialized version"""
        table, bonds = json.loads(topology_json)
        topology_df = pd.read_json(table)
        topology = md.Topology.from_dataframe(topology_df,
                                              np.array(bonds))
        return topology

    @staticmethod
    def _serialize_topology(topology):
        """Serialize MDTraj topology (to JSON)"""
        table, bonds = topology.to_dataframe()
        json_tuples = (table.to_json(), bonds.tolist())
        return json.dumps(json_tuples)

    # TODO: adding a separate object for these frozenset counters will be
    # useful for many things, and this serialization should be moved there
    @staticmethod
    def _serialize_contact_counter(counter):
        """JSON string from contact counter"""
        # have to explicitly convert to int because json doesn't know how to
        # serialize np.int64 objects, which we get in Python 3
        serializable = {json.dumps([int(val) for val in key]): counter[key]
                        for key in counter}
        return json.dumps(serializable)

    @staticmethod
    def _deserialize_contact_counter(json_string):
        """Contact counted from JSON string"""
        dct = json.loads(json_string)
        counter = collections.Counter({
            frozenset(json.loads(key)): dct[key] for key in dct
        })
        return counter

    def to_json(self):
        """JSON-serialized version of this object.

        See also
        --------
        from_json
        """
        dct = self.to_dict()
        return json.dumps(dct)

    @classmethod
    def from_json(cls, json_string):
        """Create object from JSON string

        Parameters
        ----------
        json_string : str
            JSON-serialized version of the object

        See also
        --------
        to_json
        """
        dct = json.loads(json_string)
        return cls.from_dict(dct)

    def _check_compatibility(self, other, err=AssertionError):
        compatibility_attrs = ['cutoff', 'topology', 'query', 'haystack',
                               'n_neighbors_ignored']
        failed_attr = {}
        for attr in compatibility_attrs:
            self_val = getattr(self, attr)
            other_val = getattr(other, attr)
            if self_val != other_val:
                failed_attr.update({attr: (self_val, other_val)})
        msg = "Incompatible ContactObjects:\n"
        for (attr, vals) in failed_attr.items():
            msg += "        %s: %s != %s".format(attr, str(vals[0]),
                                                 str(vals[1]))
        if failed_attr:
            raise err(msg)

    def save_to_file(self, filename, mode="w"):
        """Save this object to the given file.

        Parameters
        ----------
        filename : string
            the file to write to
        mode : 'w' or 'a'
            file writing mode. Use 'w' to overwrite, 'a' to append. Note
            that writing by bytes ('b' flag) is automatically added.

        See also
        --------
        from_file : load from generated file
        """
        with open(filename, mode+"b") as f:
            pickle.dump(self, f)

    @classmethod
    def from_file(cls, filename):
        """Load this object from a given file

        Parameters
        ----------
        filename : string
            the file to read from

        Returns
        -------
        :class:`.ContactObject`:
            the reloaded object

        See also
        --------
        save_to_file : save to a file
        """
        with open(filename, "rb") as f:
            reloaded = pickle.load(f)
        return reloaded

    def __sub__(self, other):
        return ContactDifference(positive=self, negative=other)

    @property
    def cutoff(self):
        """float : cutoff distance for contacts, in nanometers"""
        return self._cutoff

    @property
    def n_neighbors_ignored(self):
        """int : number of neighbor residues (in same chain) to ignore"""
        return self._n_neighbors_ignored

    @property
    def query(self):
        """list of int : indices of atoms to include as query"""
        return list(self._query)

    @property
    def haystack(self):
        """list of int : indices of atoms to include as haystack"""
        return list(self._haystack)

    @property
    def topology(self):
        """
        :class:`mdtraj.Topology` :
            topology object for this system

            The topology includes information about the atoms, how they are
            grouped into residues, and how the residues are grouped into
            chains.
        """
        return self._topology

    @property
    def residue_query_atom_idxs(self):
        """dict : maps query residue index to atom indices in query"""
        result = {}
        for atom_idx in self._query:
            residue_idx = self.topology.atom(atom_idx).residue.index
            try:
                result[residue_idx] += [atom_idx]
            except KeyError:
                result[residue_idx] = [atom_idx]
        return result


    @property
    def residue_ignore_atom_idxs(self):
        """dict : maps query residue index to atom indices to ignore"""
        result = {}
        for residue_idx in self.residue_query_atom_idxs.keys():
            residue = self.topology.residue(residue_idx)
            # Several steps to go residue indices -> atom indices
            ignore_residue_idxs = residue_neighborhood(
                residue,
                self._n_neighbors_ignored
            )
            ignore_residues = [self.topology.residue(idx)
                               for idx in ignore_residue_idxs]
            ignore_atoms = sum([list(res.atoms)
                                for res in ignore_residues], [])
            ignore_atom_idxs = set([atom.index for atom in ignore_atoms])
            result[residue_idx] = ignore_atom_idxs
        return result

    def most_common_atoms_for_residue(self, residue):
        """
        Most common atom contact pairs for contacts with the given residue

        Parameters
        ----------
        residue : Residue or int
            the Residue object or index representing the residue for which
            the most common atom contact pairs will be calculated

        Returns
        -------
        list :
            Atom contact pairs involving given residue, order of frequency.
            Referring to the list as ``l``, each element of the list
            ``l[e]`` consists of two parts: ``l[e][0]`` is a list containing
            the two MDTraj Atom objects that make up the contact, and
            ``l[e][1]`` is the measure of how often the contact occurs.
        """
        residue = _residue_and_index(residue, self.topology)[0]
        residue_atoms = set(atom.index for atom in residue.atoms)
        results = []
        for atoms, number in self.atom_contacts.most_common_idx():
            atoms_in_residue = atoms & residue_atoms
            if atoms_in_residue:
                as_atoms = [self.topology.atom(a) for a in atoms]
                results += [(as_atoms, number)]

        return results

    def most_common_atoms_for_contact(self, contact_pair):
        """
        Most common atom contacts for a given residue contact pair

        Parameters
        ----------
        contact_pair : length 2 list of Residue or int
            the residue contact pair for which the most common atom contact
            pairs will be calculated

        Returns
        -------
        list :
            Atom contact pairs for the residue contact pair, in order of
            frequency.  Referring to the list as ``l``, each element of the
            list ``l[e]`` consists of two parts: ``l[e][0]`` is a list
            containing the two MDTraj Atom objects that make up the contact,
            and ``l[e][1]`` is the measure of how often the contact occurs.
        """
        contact_pair = list(contact_pair)
        res_1 = _residue_and_index(contact_pair[0], self.topology)[0]
        res_2 = _residue_and_index(contact_pair[1], self.topology)[0]
        atom_idxs_1 = set(atom.index for atom in res_1.atoms)
        atom_idxs_2 = set(atom.index for atom in res_2.atoms)
        all_atom_pairs = [
            frozenset(pair)
            for pair in itertools.product(atom_idxs_1, atom_idxs_2)
        ]
        result = [([self.topology.atom(idx) for idx in contact[0]], contact[1])
                  for contact in self.atom_contacts.most_common_idx()
                  if frozenset(contact[0]) in all_atom_pairs]
        return result


    def contact_map(self, trajectory, frame_number, residue_query_atom_idxs,
                    residue_ignore_atom_idxs):
        """
        Returns atom and residue contact maps for the given frame.

        Parameters
        ----------
        frame : mdtraj.Trajectory
            the desired frame (uses the first frame in this trajectory)
        residue_query_atom_idxs : dict
        residue_ignore_atom_idxs : dict

        Returns
        -------
        atom_contacts : collections.Counter
        residue_contact : collections.Counter
        """
        neighborlist = md.compute_neighborlist(trajectory, self.cutoff,
                                               frame_number)
        contact_pairs = set([])
        residue_pairs = set([])
        for residue_idx in residue_query_atom_idxs:
            ignore_atom_idxs = set(residue_ignore_atom_idxs[residue_idx])
            query_idxs = residue_query_atom_idxs[residue_idx]
            for atom_idx in query_idxs:
                # sets should make this fast, esp since neighbor_idxs
                # should be small and s-t is avg cost len(s)
                neighbor_idxs = set(neighborlist[atom_idx])
                contact_neighbors = neighbor_idxs - ignore_atom_idxs
                contact_neighbors = contact_neighbors & self._haystack
                # frozenset is unique key independent of order
                # local_pairs = set(frozenset((atom_idx, neighb))
                #                   for neighb in contact_neighbors)
                local_pairs = set(map(
                    frozenset,
                    itertools.product([atom_idx], contact_neighbors)
                ))
                contact_pairs |= local_pairs
                # contact_pairs |= set(frozenset((atom_idx, neighb))
                #                      for neighb in contact_neighbors)
                local_residue_partners = set(self._atom_idx_to_residue_idx[a]
                                             for a in contact_neighbors)
                local_res_pairs = set(map(
                    frozenset,
                    itertools.product([residue_idx], local_residue_partners)
                ))
                residue_pairs |= local_res_pairs

        atom_contacts = collections.Counter(contact_pairs)
        # residue_pairs = set(
        #     frozenset(self._atom_idx_to_residue_idx[aa] for aa in pair)
        #     for pair in contact_pairs
        # )
        residue_contacts = collections.Counter(residue_pairs)
        return (atom_contacts, residue_contacts)

    @property
    def atom_contacts(self):
        n_atoms = self.topology.n_atoms
        return ContactCount(self._atom_contacts, self.topology.atom,
                            n_atoms, n_atoms)

    @property
    def residue_contacts(self):
        n_res = self.topology.n_residues
        return ContactCount(self._residue_contacts, self.topology.residue,
                            n_res, n_res)


class ContactMap(ContactObject):
    """
    Contact map (atomic and residue) for a single frame.
    """
    def __init__(self, frame, query=None, haystack=None, cutoff=0.45,
                 n_neighbors_ignored=2):
        self._frame = frame  # TODO: remove this?
        super(ContactMap, self).__init__(frame.topology, query, haystack,
                                         cutoff, n_neighbors_ignored)
        contact_maps = self.contact_map(frame, 0,
                                        self.residue_query_atom_idxs,
                                        self.residue_ignore_atom_idxs)
        (self._atom_contacts, self._residue_contacts) = contact_maps

    def __hash__(self):
        return hash((super(ContactMap, self).__hash__(),
                     tuple(self._atom_contacts.items()),
                     tuple(self._residue_contacts.items())))

    def __eq__(self, other):
        is_equal = (super(ContactMap, self).__eq__(other)
                    and self._atom_contacts == other._atom_contacts
                    and self._residue_contacts == other._residue_contacts)
        return is_equal


class ContactFrequency(ContactObject):
    """
    Contact frequency (atomic and residue) for a trajectory.

    The contact frequency is defined as fraction of the trajectory that a
    certain contact is made. This object calculates this quantity for all
    contacts with atoms in the `query` residue, with "contact" defined as
    being within a certain cutoff distance.

    Parameters
    ----------
    trajectory : mdtraj.Trajectory
        Trajectory (segment) to analyze
    query : list of int
        Indices of the atoms to be included as query. Default ``None``
        means all atoms.
    haystack : list of int
        Indices of the atoms to be included as haystack. Default ``None``
        means all atoms.
    cutoff : float
        Cutoff distance for contacts, in nanometers. Default 0.45.
    n_neighbors_ignored : int
        Number of neighboring residues (in the same chain) to ignore.
        Default 2.
    """
    def __init__(self, trajectory, query=None, haystack=None, cutoff=0.45,
                 n_neighbors_ignored=2, frames=None):
        if frames is None:
            frames = range(len(trajectory))
        self.frames = frames
        self._n_frames = len(frames)
        super(ContactFrequency, self).__init__(trajectory.topology,
                                               query, haystack, cutoff,
                                               n_neighbors_ignored)
        contacts = self._build_contact_map(trajectory)
        (self._atom_contacts, self._residue_contacts) = contacts

    def __hash__(self):
        return hash((super(ContactFrequency, self).__hash__(),
                     tuple(self._atom_contacts.items()),
                     tuple(self._residue_contacts.items()),
                     self.n_frames))

    def __eq__(self, other):
        is_equal = (super(ContactFrequency, self).__eq__(other)
                    and self._atom_contacts == other._atom_contacts
                    and self._residue_contacts == other._residue_contacts
                    and self.n_frames == other.n_frames)
        return is_equal

    def to_dict(self):
        dct = super(ContactFrequency, self).to_dict()
        dct.update({'n_frames': self.n_frames})
        return dct

    def _build_contact_map(self, trajectory):
        # We actually build the contact map on a per-residue basis, although
        # we save it on a per-atom basis. This allows us ignore
        # n_nearest_neighbor residues.
        # TODO: this whole thing should be cleaned up and should replace
        # MDTraj's really slow old compute_contacts by using MDTraj's new
        # neighborlists (unless the MDTraj people do that first).
        atom_contacts_count = collections.Counter([])
        residue_contacts_count = collections.Counter([])

        # cache things that can be calculated once based on the topology
        # (namely, which atom indices matter for each residue)
        residue_ignore_atom_idxs = self.residue_ignore_atom_idxs
        residue_query_atom_idxs = self.residue_query_atom_idxs
        for frame_num in self.frames:
            frame_contacts = self.contact_map(trajectory, frame_num,
                                              residue_query_atom_idxs,
                                              residue_ignore_atom_idxs)
            frame_atom_contacts = frame_contacts[0]
            frame_residue_contacts = frame_contacts[1]
            # self._atom_contacts_count += frame_atom_contacts
            atom_contacts_count.update(frame_atom_contacts)
            residue_contacts_count += frame_residue_contacts

        return (atom_contacts_count, residue_contacts_count)

    @property
    def n_frames(self):
        """Number of frames in the mapped trajectory"""
        return self._n_frames

    def add_contact_frequency(self, other):
        """Add results from `other` to the internal counter.

        Parameters
        ----------
        other : :class:`.ContactFrequency`
            contact frequency made from the frames to remove from this
            contact frequency
        """
        self._check_compatibility(other)
        self._atom_contacts += other._atom_contacts
        self._residue_contacts += other._residue_contacts
        self._n_frames += other._n_frames


    def subtract_contact_frequency(self, other):
        """Subtracts results from `other` from internal counter.

        Note that this is intended for the case that you're removing a
        subtrajectory of the already-calculated trajectory. If you want to
        compare two different contact frequency maps, use
        :class:`.ContactDifference`.

        Parameters
        ----------
        other : :class:`.ContactFrequency`
            contact frequency made from the frames to remove from this
            contact frequency
        """
        self._check_compatibility(other)
        self._atom_contacts -= other._atom_contacts
        self._residue_contacts -= other._residue_contacts
        self._n_frames -= other._n_frames

    @property
    def atom_contacts(self):
        """Atoms pairs mapped to fraction of trajectory with that contact"""
        n_x = self.topology.n_atoms
        n_y = self.topology.n_atoms
        return ContactCount(collections.Counter({
            item[0]: float(item[1])/self.n_frames
            for item in self._atom_contacts.items()
        }), self.topology.atom, n_x, n_y)

    @property
    def residue_contacts(self):
        """Residue pairs mapped to fraction of trajectory with that contact"""
        n_x = self.topology.n_residues
        n_y = self.topology.n_residues
        return ContactCount(collections.Counter({
            item[0]: float(item[1])/self.n_frames
            for item in self._residue_contacts.items()
        }), self.topology.residue, n_x, n_y)


class ContactDifference(ContactObject):
    """
    Contact map comparison (atomic and residue).

    This can compare single frames or entire trajectories (or even mix the
    two!) While this can be directly instantiated by the user, the more
    common way to make this object is by using the ``-`` operator, i.e.,
    ``diff = map_1 - map_2``.
    """
    def __init__(self, positive, negative):
        self.positive = positive
        self.negative = negative
        positive._check_compatibility(negative)
        super(ContactDifference, self).__init__(positive.topology,
                                                positive.query,
                                                positive.haystack,
                                                positive.cutoff,
                                                positive.n_neighbors_ignored)

    def to_dict(self):
        """Convert object to a dict.

        Keys should be strings; values should be (JSON-) serializable.

        See also
        --------
        from_dict
        """
        return {
            'positive': self.positive.to_json(),
            'negative': self.negative.to_json(),
            'positive_cls': self.positive.__class__.__name__,
            'negative_cls': self.negative.__class__.__name__
        }

    @classmethod
    def from_dict(cls, dct):
        """Create object from dict.

        Parameters
        ----------
        dct : dict
            dict-formatted serialization (see to_dict for details)

        See also
        --------
        to_dict
        """
        # TODO: add searching for subclasses (http://code.activestate.com/recipes/576949-find-all-subclasses-of-a-given-class/)
        supported_classes = [ContactMap, ContactFrequency]
        supported_classes_dict = {class_.__name__: class_
                                  for class_ in supported_classes}

        def rebuild(pos_neg):
            class_name = dct[pos_neg + "_cls"]
            try:
                cls_ = supported_classes_dict[class_name]
            except KeyError:  # pragma: no cover
                raise RuntimeError("Can't rebuild class " + class_name)
            obj = cls_.from_json(dct[pos_neg])
            return obj

        positive = rebuild('positive')
        negative = rebuild('negative')
        return cls(positive, negative)

    def __sub__(self, other):
        raise NotImplementedError

    def contact_map(self, *args, **kwargs):  #pylint: disable=W0221
        raise NotImplementedError

    @property
    def atom_contacts(self):
        n_x = self.topology.n_atoms
        n_y = self.topology.n_atoms
        diff = collections.Counter(self.positive.atom_contacts.counter)
        diff.subtract(self.negative.atom_contacts.counter)
        return ContactCount(diff, self.topology.atom, n_x, n_y)

    @property
    def residue_contacts(self):
        n_x = self.topology.n_residues
        n_y = self.topology.n_residues
        diff = collections.Counter(self.positive.residue_contacts.counter)
        diff.subtract(self.negative.residue_contacts.counter)
        return ContactCount(diff, self.topology.residue, n_x, n_y)
