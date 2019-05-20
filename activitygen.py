#!/usr/bin/env python3

""" SUMO Activity-Based Mobility Generator

    Author: Lara CODECA

    This program and the accompanying materials are made available under the
    terms of the Eclipse Public License 2.0 which is available at
    http://www.eclipse.org/legal/epl-2.0.
"""

import argparse
import collections
import cProfile
import csv
import io
import json
import logging
import os
import pstats
import sys
import xml.etree.ElementTree

from enum import Enum

import numpy
from numpy.random import RandomState
from tqdm import tqdm

# """ Import SUMOLIB """
if 'SUMO_TOOLS' in os.environ:
    sys.path.append(os.environ['SUMO_TOOLS'])
    import sumolib
    import traci
    import traci.constants as tc
else:
    sys.exit("Please declare environment variable 'SUMO_TOOLS'")

BASE_DIR = None
if 'MOBILITY_GENERATOR' in os.environ:
    BASE_DIR = os.environ['MOBILITY_GENERATOR']
else:
    sys.exit("Please declare environment variable 'MOBILITY_GENERATOR'")

def _logs():
    """ Log init. """
    file_handler = logging.FileHandler(filename='{}.log'.format(sys.argv[0]),
                                       mode='w')
    stdout_handler = logging.StreamHandler(sys.stdout)
    handlers = [file_handler, stdout_handler]
    logging.basicConfig(handlers=handlers, level=logging.INFO,
                        format='[%(asctime)s] %(levelname)s: %(message)s',
                        datefmt='%m/%d/%Y %I:%M:%S %p')

def _args():
    """
    Argument Parser
    ret: parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog='{}'.format(sys.argv[0]),
        usage='%(prog)s -c configuration.json',
        description='SUMO Activity-Based Mobility Generator')
    parser.add_argument(
        '-c', type=str, dest='config', required=True,
        help='JSON configuration file.')
    parser.add_argument(
        '--profiling', dest='profiling', action='store_true',
        help='Enable Python3 cProfile feature.')
    parser.add_argument(
        '--no-profiling', dest='profiling', action='store_false',
        help='Disable Python3 cProfile feature.')
    parser.set_defaults(profiling=False)
    return parser.parse_args()

def _load_configurations(filename):
    """
    Load JSON configuration file in a dict.
        :param filename: name of the JSON file containing the configuarions.
    """
    return json.loads(open(filename).read())

## For SUMO API compatibility
Stage = collections.namedtuple(
    'Stage',
    ['type', 'vType', 'line', 'destStop', 'edges', 'travelTime', 'cost', 'length', 'intended',
     'depart', 'departPos', 'arrivalPos', 'description'],
    defaults=('',) * 13)

## Activity
Activity = collections.namedtuple(
    'Activity',
    ['activity', 'fromEdge', 'toEdge', 'arrivalPos', 'start', 'duration'],
    defaults=(None,) * 6)

class TripGenerationGenericError(Exception):
    """ During the trip generation, various erroroneous states can be reached.
    """
    message = None
    def __init__(self, message=None):
        """ Init the error message. """
        super().__init__()
        self.message = message
        if self.message:
            logging.debug(self.message)

class TripGenerationActivityError(TripGenerationGenericError):
    """ During the generation from the activity chains, various erroroneous states can be reached.
    """
    message = None
    def __init__(self, message=None):
        """ Init the error message. """
        super().__init__()
        self.message = message
        if self.message:
            logging.error(self.message)

class TripGenerationRouteError(TripGenerationGenericError):
    """ During the step by step generation of the trip, it is possible to reach a state in which
        some of the chosen locations are impossible to reach.
    """
    message = None
    def __init__(self, message=None):
        """ Init the error message. """
        super().__init__()
        self.message = message
        if self.message:
            logging.debug(self.message)

class TripGenerationInconsistencyError(TripGenerationGenericError):
    """ During the step by step generation of the trip, it is possible to reach a state in which
        some of the chosen modes are impossible to be used in that order.
    """
    message = None
    def __init__(self, message=None):
        """ Init the error message. """
        super().__init__()
        self.message = message
        if self.message:
            logging.error(self.message)

class ModeShare(Enum):
    """ Selector between two interpretation of the values used for the modes:
        - PROBABILITY: only one mode is selected using the given probability.
        - WEIGHT: all the modes are generated, the cost is multiplied by the given weight and
                    only the cheapest solution is used.
    """
    PROBABILITY = 1
    WEIGHT = 2

class MobilityGenerator(object):
    """ Generates intermodal mobility for SUMO starting from a synthetic population. """

    _conf = None
    _profiling = None

    _mode_interpr = None

    _random_generator = None

    _sumo_network = None
    _sumo_parkings = collections.defaultdict(list)
    _parking_cache = dict()
    _parking_position = dict()
    _taz_weights = dict()
    _buildings_by_taz = dict()
    _edges_by_taz = dict()

    _blacklisted_edges = set()

    _all_trips = collections.defaultdict(dict)

    def __init__(self, conf, profiling=False):
        """
         Initialize the synthetic population.
            :param conf: distionary with the configurations
            :param profiling=False: enable cProfile
        """

        self._conf = conf

        if not conf['population']['modeSelection']:
            raise Exception('The parameter "modeSelection" in "population" must be defined.')
        elif conf['population']['modeSelection'] == 'PROBABILITY':
            self._mode_interpr = ModeShare.PROBABILITY
        elif conf['population']['modeSelection'] == 'WEIGHT':
            self._mode_interpr = ModeShare.WEIGHT
        else:
            raise Exception('The parameter "modeSelection" in "population" must be set to '
                            '"PROBABILITY" or "WEIGHT".')

        self._profiling = profiling

        self._random_generator = RandomState(seed=self._conf['seed'])

        logging.info('Starting TraCI with file %s.', conf['sumocfg'])
        sumocfg = '{}/{}'.format(BASE_DIR, conf['sumocfg'])
        traci.start(['sumo', '-c', sumocfg])

        logging.info('Loading SUMO net file %s%s', BASE_DIR, conf['SUMOnetFile'])
        self._sumo_network = sumolib.net.readNet(
            '{}/{}'.format(BASE_DIR, conf['SUMOnetFile']))

        logging.info('Loading SUMO parking lots from file %s%s',
                     BASE_DIR, conf['SUMOadditionals']['parkings'])
        self._load_parkings('{}/{}'.format(BASE_DIR, conf['SUMOadditionals']['parkings']))

        logging.info('Loading TAZ weights from %s%s',
                     BASE_DIR, conf['population']['tazWeights'])
        self._load_weights_from_csv(
            '{}/{}'.format(BASE_DIR, conf['population']['tazWeights']))

        logging.info('Loading buildings weights from %s%s',
                     BASE_DIR, conf['population']['buildingsWeight'])
        self._load_buildings_weight_from_csv_dir(
            '{}/{}'.format(BASE_DIR, conf['population']['buildingsWeight']))

        logging.info('Loading edges in each TAZ from %s%s',
                     BASE_DIR, conf['population']['tazDefinition'])
        self._load_edges_from_taz(
            '{}/{}'.format(BASE_DIR, conf['population']['tazDefinition']))

        logging.info('Computing the number of entities for each mobility slice..')
        self._compute_entities_per_slice()

    def mobility_generation(self):
        """ Generate the mobility for the synthetic population. """
        logging.info('Generating trips for each mobility slice..')
        self._compute_trips_per_slice()

    def save_mobility(self):
        """ Save the generated trips to files. """
        logging.info('Saving trips files..')
        self._saving_trips_to_files()

    @staticmethod
    def close_traci():
        """ Artefact to close TraCI properly. """
        logging.info('Closing TraCI.')
        traci.close()

    ## ---------------------------------------------------------------------------------------- ##
    ##                                          Loaders                                         ##
    ## ---------------------------------------------------------------------------------------- ##

    def _load_parkings(self, filename):
        """ Load parkings ids from XML file. """
        xml_tree = xml.etree.ElementTree.parse(filename).getroot()
        for child in xml_tree:
            if (child.tag == 'parkingArea' and
                    child.attrib['id'] in self._conf['intermodalOptions']['parkingAreaWhitelist']):
                edge = child.attrib['lane'].split('_')[0]
                position = float(child.attrib['startPos']) + 2.5
                self._sumo_parkings[edge].append(child.attrib['id'])
                self._parking_position[child.attrib['id']] = position

    def _load_weights_from_csv(self, filename):
        """ Load the TAZ weight from a CSV file. """
        with open(filename, 'r') as csvfile:
            weightreader = csv.reader(csvfile)
            header = None
            for row in weightreader:
                if not header:
                    header = row
                else:
                    self._taz_weights[int(row[0])] = {
                        header[0]: int(row[0]),
                        header[1]: row[1],
                        header[2]: int(row[2]),
                        header[3]: float(row[3]),
                        'weight': (int(row[2])/float(row[3])),
                    }

    def _load_buildings_weight_from_csv_dir(self, directory):
        """ Load the buildings weight from multiple CSV files. """

        allfiles = [os.path.join(directory, f)
                    for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))]
        for filename in sorted(allfiles):
            logging.debug('Loding %s', filename)
            with open(filename, 'r') as csvfile:
                weightreader = csv.reader(csvfile)
                header = None
                taz = None
                buildings = []
                for row in weightreader:
                    if not header:
                        header = row
                    else:
                        taz = row[0]
                        buildings.append((float(row[3]),    # weight
                                          row[4],           # generic edge
                                          row[5]))          # pedestrian edge

                if len(buildings) < 10:
                    logging.debug('Dropping %s, only %d buildings found.', filename, len(buildings))
                    continue

                weighted_buildings = []
                cum_sum = 0.0
                for weight, g_edge, p_edge in sorted(buildings):
                    cum_sum += weight
                    weighted_buildings.append((cum_sum, g_edge, p_edge, weight))
                self._buildings_by_taz[taz] = weighted_buildings

    def _load_edges_from_taz(self, filename):
        """ Load edges from the TAZ file. """
        xml_tree = xml.etree.ElementTree.parse(filename).getroot()
        for child in xml_tree:
            if child.tag == 'taz':
                self._edges_by_taz[child.attrib['id']] = child.attrib['edges'].split(' ')

    ## ---------------------------------------------------------------------------------------- ##
    ##                                Mobility Generation                                       ##
    ## ---------------------------------------------------------------------------------------- ##

    def _compute_entities_per_slice(self):
        """
        Compute the absolute number of entities that are going to be created
        for each moblitiy slice, given a population.
        """
        logging.info('Population: %d', self._conf['population']['entities'])

        for m_slice in self._conf['slices'].keys():
            self._conf['slices'][m_slice]['tot'] = int(
                self._conf['population']['entities'] * self._conf['slices'][m_slice]['perc'])
            logging.info('\t %s: %d', m_slice, self._conf['slices'][m_slice]['tot'])

    def _compute_trips_per_slice(self):
        """ Compute the trips for the synthetic population for each mobility slice. """

        total = 0

        _modes_stats = collections.defaultdict(int)

        for name, m_slice in self._conf['slices'].items():
            logging.info('[%s] Computing %d trips from %s to %s ... ',
                         name, m_slice['tot'], m_slice['loc_origin'], m_slice['loc_primary'])

            ## Activity chains preparation
            activity_chains = []
            activity_chains_weights = []
            for _weight, _chain, _modes in m_slice['activityChains']:
                activity_chains.append((_chain, _modes))
                activity_chains_weights.append(_weight)
            activity_index = [i for i in range(len(activity_chains))]

            if self._profiling:
                _pr = cProfile.Profile()
                _pr.enable()

            for entity_id in tqdm(range(m_slice['tot'])):
                ## Select the activity chain
                _index = self._random_generator.choice(
                    activity_index, p=activity_chains_weights)
                _chain, _modes = activity_chains[_index]
                logging.debug('Chain: %s', '{}'.format(_chain))
                logging.debug('Modes: %s', '{}'.format(_modes))

                _person_trip = None

                # (Intermodal) trip
                _final_chain = None
                _stages = None
                _error_counter = 0
                while not _person_trip:
                    try:
                        _final_chain, _stages, _selected_mode = self._generate_trip_traci(
                            self._conf['taz'][m_slice['loc_origin']],
                            self._conf['taz'][m_slice['loc_primary']],
                            _chain, _modes)

                        ## Generating departure time
                        _depart = numpy.round(_final_chain[1].start, decimals=2)
                        if _depart < 0.0:
                            raise TripGenerationGenericError('Negative departure time.')
                        if _depart not in self._all_trips[name].keys():
                            self._all_trips[name][_depart] = []

                        ## fix the last stop with 1.0 duration
                        if _stages[-1].type == tc.STAGE_WAITING:
                            _stages[-1] = _stages[-1]._replace(travelTime=1.0)
                            _stages[-1] = _stages[-1]._replace(cost=1.0)

                        ## fix the last ride with cost = -42.42 on order to fix the last stop
                        _pos = len(_stages) - 1
                        while _pos >= 0:
                            if  _stages[_pos].type == tc.STAGE_DRIVING:
                                if not  _stages[_pos].destStop:
                                    _stages[_pos] = _stages[_pos]._replace(travelTime=-42.42)
                                    _stages[_pos] = _stages[_pos]._replace(cost=-42.42)
                                    break
                            _pos -= 1

                        _person_trip = {
                            'id': '{}_{}'.format(name, entity_id),
                            'depart': _depart,
                            'stages': _stages,
                        }

                        complete_trip = self._generate_sumo_trip_from_activitygen(_person_trip)
                        _person_trip['string'] = complete_trip
                        ## For statistical purposes.
                        _modes_stats[_selected_mode] += 1

                    except TripGenerationGenericError:
                        _person_trip = None
                        _error_counter += 1
                        if _error_counter % 10 == 0:
                            logging.error(
                                '_generate_trip_traci from %s to %s generated %d errors'
                                ' and counting..',
                                self._conf['taz'][m_slice['loc_origin']],
                                self._conf['taz'][m_slice['loc_primary']],
                                _error_counter)

                # Trip creation
                self._all_trips[name][_depart].append(_person_trip)
                total += 1

            if self._profiling:
                _pr.disable()
                _s = io.StringIO()
                _ps = pstats.Stats(_pr, stream=_s).sort_stats('cumulative')
                _ps.print_stats(10)
                print(_s.getvalue())
                input("Press any key to continue..")

        logging.info('Generated %d trips.', total)
        for mode, value in _modes_stats.items():
            logging.info('\t %s: %d (%.2f).', mode, value, float(value/total))

    ## ---- PARKING AREAS: location and selection ---- ##

    def _check_parkings_cache(self, edge):
        """ Check among the previously computed results of _find_closest_parking """
        if edge in self._parking_cache.keys():
            return self._parking_cache[edge]
        return None

    def _find_closest_parking(self, edge):
        """ Given and edge, find the closest parking area. """
        distance = sys.float_info.max

        ret = self._check_parkings_cache(edge)
        if ret:
            return ret

        p_id = None

        for p_edge, parkings in self._sumo_parkings.items():
            for parking in parkings:
                if parking in self._conf['intermodalOptions']['parkingAreaWhitelist']:
                    p_id = parking
                    break
            if p_id:
                try:
                    route = traci.simulation.findIntermodalRoute(
                        p_edge, edge, pType="pedestrian")
                except traci.exceptions.TraCIException:
                    route = None
                if route:
                    cost = self._cost_from_route(route)
                    if distance > cost:
                        distance = cost
                        ret = p_id, p_edge, route

        if ret:
            self._parking_cache[edge] = ret
            return ret

        logging.fatal('Edge %s is not reachable from any parking lot.', edge)
        self._blacklisted_edges.add(edge)
        return None, None, None

    ## ---- Functions for _compute_trips_per_slice: _generate_trip_traci ---- ##

    def _generate_trip_traci(self, from_area, to_area, activity_chain, modes):
        """ Returns the trip for the given activity chain. """

        trip = None
        solutions = []

        _interpr_modes = None
        if self._mode_interpr == ModeShare.PROBABILITY:
            _probs = []
            _vals = []
            for mode, prob in modes:
                _vals.append(mode)
                _probs.append(prob)
            selection = self._random_generator.choice(_vals, p=_probs)
            _interpr_modes = [[selection, 1.0]] ## Unique mode, with 'no' weight.
        else:
            _interpr_modes = modes

        for mode, weight in _interpr_modes:

            _person_stages = self._generate_person_stages(from_area, to_area, activity_chain, mode)

            _person_steps = []
            _new_start_time = None

            _mode, _ptype, _vtype = self._get_mode_parameters(mode)

            for pos, stage in _person_stages.items():
                # findIntermodalRoute(self, fromEdge, toEdge, modes='', depart=-1.0,
                #                     routingMode=0, speed=-1.0, walkFactor=-1.0,
                #                     departPos=0.0, arrivalPos=-1073741824, departPosLat=0.0,
                #                     pType='', vType='', destStop='')
                if not _new_start_time:
                    _new_start_time = stage.start

                route = None

                ## If the vtype is among the one that require parking, and we are not going home,
                #  look for a parking and build the additional walk back and forth.
                if (stage.activity != 'Home' and
                        _vtype in self._conf['intermodalOptions']['vehicleAllowedParking']):
                    ## find parking
                    p_id, p_edge, _last_mile = self._find_closest_parking(stage.toEdge)
                    if _last_mile:
                        route = traci.simulation.findIntermodalRoute(
                            stage.fromEdge, p_edge, depart=_new_start_time, walkFactor=.9,
                            modes=_mode, pType=_ptype, vType=_vtype)

                        if (self._is_valid_route(mode, route) and
                                route[-1].type == tc.STAGE_DRIVING):
                            route[-1] = route[-1]._replace(destStop=p_id)
                            route[-1] = route[-1]._replace(arrivalPos=self._parking_position[p_id])
                            route.extend(_last_mile)
                        else:
                            route = None
                    if route:
                        ## build the waiting to destination (if required)
                        if stage.duration:
                            wait = self._generate_waiting_stage(stage)
                            route.append(wait)

                        ## build the walk back to the parking
                        walk_back = traci.simulation.findIntermodalRoute(
                            stage.toEdge, p_edge, walkFactor=.9, pType="pedestrian")
                        walk_back = walk_back[0]._replace(arrivalPos=self._parking_position[p_id])
                        route.append(walk_back)

                        ## update the next stage to make it start from the parking
                        if pos + 1 in _person_stages:
                            _person_stages[pos+1] = _person_stages[pos+1]._replace(fromEdge=p_edge)
                else:
                    ## PUBLIC, ON-DEMAND, trip to HOME, and NO-PARKING required vehicles.
                    route = traci.simulation.findIntermodalRoute(
                        stage.fromEdge, stage.toEdge, depart=_new_start_time, walkFactor=.9,
                        modes=_mode, pType=_ptype, vType=_vtype)
                    if not self._is_valid_route(mode, route):
                        route = None
                    if route:
                        ## Set the arrival position in the edge
                        route[-1] = route[-1]._replace(arrivalPos=stage.arrivalPos)
                        ## Add stop
                        if stage.duration:
                            route.append(self._generate_waiting_stage(stage))

                if not route:
                    raise TripGenerationRouteError(
                        'Route not found between {} and {}.'.format(stage.fromEdge, stage.toEdge))

                ## Add the stage to the full planned trip.
                for step in route:
                    _new_start_time += step.travelTime
                    _person_steps.append(step)

            ## Cost computation.
            solutions.append(
                (self._cost_from_route(_person_steps) * weight,
                 _person_steps, _person_stages, mode))

        ## Compose the final person trip.
        if solutions:
            ## For the moment, the best solution is the one with minor cost.
            best = sorted(solutions)[0] ## Ascending.
            trip = (best[2], best[1], best[3]) ## _person_stages, _person_steps, mode
        else:
            raise TripGenerationRouteError('No solution foud for chain {} and modes {}.'.format(
                activity_chain, _interpr_modes))
        return trip

    @staticmethod
    def _generate_waiting_stage(stage):
        """ Builds a STAGE_WAITING type of stage compatible with findIntermodalRoute. """
        wait = Stage(
            type=tc.STAGE_WAITING, description=stage.activity,
            edges='{}_0'.format(stage.toEdge), travelTime=stage.duration,
            cost=stage.duration)
        return wait

    def _stages_define_main_locations(self, from_area, to_area, mode):
        """ Define a generic Home and Primary activity location.
            The locations must be reachable in some ways.
        """
        ## Mode split:
        _mode, _ptype, _vtype = self._get_mode_parameters(mode)

        route = None
        from_edge = None
        to_edge = None
        while not route:
            ## Origin and Destination Selection
            from_edge, to_edge = self._select_pair(from_area, to_area)
            from_allowed = (
                self._sumo_network.getEdge(from_edge).allows('pedestrian') and
                self._sumo_network.getEdge(from_edge).allows('passenger') and
                self._sumo_network.getEdge(from_edge).getLength() > self._conf['minEdgeAllowed'])
            to_allowed = (
                self._sumo_network.getEdge(to_edge).allows('pedestrian') and
                self._sumo_network.getEdge(to_edge).allows('passenger') and
                self._sumo_network.getEdge(to_edge).getLength() > self._conf['minEdgeAllowed'])
            if self._valid_pair(from_edge, to_edge) and from_allowed and to_allowed:
                try:
                    route = traci.simulation.findIntermodalRoute(
                        from_edge, to_edge, modes=_mode, pType=_ptype, vType=_vtype)
                    if not self._is_valid_route(mode, route):
                        route = None
                except traci.exceptions.TraCIException:
                    logging.debug('_generate_person_stages: findRoute FAILED.')
                    route = None
            else:
                logging.debug('_generate_person_stages: unusable pair of edges.')
        return from_edge, to_edge

    def _stages_define_secondary_locations(self, person_stages, home, primary):
        """ Define secondary activity locations. """
        for pos, stage in person_stages.items():
            if  'S-' in stage.activity:
                ## look for what is coming before
                _prec = None
                _pos = pos - 1
                while not _prec and _pos in person_stages:
                    if 'Home' in person_stages[_pos].activity:
                        _prec = 'H'
                    elif 'P-' in person_stages[_pos].activity:
                        _prec = 'P'
                    _pos -= 1

                ## look for what is coming next
                _succ = None
                _pos = pos + 1
                while not _succ and _pos in person_stages:
                    if 'Home' in person_stages[_pos].activity:
                        _succ = 'H'
                    elif 'P-' in person_stages[_pos].activity:
                        _succ = 'P'
                    _pos += 1

                destination = None
                if _prec == 'H' and _succ == 'H':
                    destination = self._random_location_circle(center=home, other=primary)
                elif _prec == 'P' and _succ == 'P':
                    destination = self._random_location_circle(center=primary, other=home)
                elif _prec != _succ:
                    destination = self._random_location_ellipse(home, primary)
                else:
                    raise TripGenerationActivityError(
                        'Invalid sequence in the activity chain: {} --> {}'.format(_prec, _succ))

                person_stages[pos] = stage._replace(toEdge=destination)
        return person_stages

    def _stages_compute_start_time(self, person_stages):
        """ Compute the real starting time for the activity chain. """
        # Find the first 'start' defined.
        pos = 1
        while pos in person_stages:
            if person_stages[pos].start:
                break
            pos += 1

        start = person_stages[pos].start
        while pos in person_stages:
            ett = 1800.0    # generic standard value
            try:
                ett = traci.simulation.findRoute(
                    person_stages[pos].fromEdge, person_stages[pos].toEdge).travelTime
            except traci.exceptions.TraCIException:
                pass
            if pos-1 in person_stages:
                if person_stages[pos-1].duration:
                    ett += person_stages[pos-1].duration
            start -= ett
            pos -= 1
        return start

    def _get_random_pos_from_edge(self, edge):
        """ Return a random position in the given edge. """
        length = self._sumo_network.getEdge(edge).getLength()
        position = None
        if length < self._conf['stopBufferDistance']:
            position = length/2.0

        # avoid the proximity of the intersection
        begin = self._conf['stopBufferDistance'] / 2.0
        end = length - begin
        position = (end - begin) * self._random_generator.random_sample() + begin
        return position

    def _stages_define_locations_position(self, person_stages):
        """ Define the position of each location in the activity chain. """
        home_pos = None
        primary_pos = None

        for pos, stage in person_stages.items():
            if 'Home' in stage.activity:
                if not home_pos:
                    home_pos = self._get_random_pos_from_edge(stage.toEdge)
                person_stages[pos] = stage._replace(arrivalPos=home_pos)
            elif 'P-' in stage.activity:
                if not primary_pos:
                    primary_pos = self._get_random_pos_from_edge(stage.toEdge)
                person_stages[pos] = stage._replace(arrivalPos=primary_pos)
            else:
                ## Secondary activities
                person_stages[pos] = stage._replace(
                    arrivalPos=self._get_random_pos_from_edge(stage.toEdge))

        return person_stages

    def _generate_person_stages(self, from_area, to_area, activity_chain, mode):
        """ Returns the trip for the given activity chain. """

        # Define a generic Home and Primary activity location.
        from_edge, to_edge = self._stages_define_main_locations(from_area, to_area, mode)

        ## Generate perliminary stages for a person
        person_stages = dict()
        for pos, activity in enumerate(activity_chain):
            if activity not in self._conf['activities']:
                raise Exception('Activity {} is not define in the config file.'.format(activity))
            _start, _duration = self._get_timing_from_activity(activity)
            if pos == 0:
                if activity != 'Home':
                    raise Exception("Every activity chain MUST start with 'Home',"
                                    " '{}' given.".format(activity))
                ## Beginning
                person_stages[pos] = Activity(
                    activity=activity, fromEdge=from_edge, start=_start, duration=_duration)
            elif 'P-' in activity:
                ## This is a primary activity
                person_stages[pos] = Activity(
                    activity=activity, toEdge=to_edge, start=_start, duration=_duration)
            elif 'S-' in activity:
                ## This is a secondary activity
                person_stages[pos] = Activity(
                    activity=activity, start=_start, duration=_duration)
            elif activity == 'Home':
                ## End of the activity chain.
                person_stages[pos] = Activity(
                    activity=activity, toEdge=from_edge, start=_start, duration=_duration)

        if len(person_stages) <= 2:
            raise Exception("Invalid activity chain. (Minimal: H -> P-? -> H", activity_chain)

        ## Define secondary activity location
        person_stages = self._stages_define_secondary_locations(person_stages, from_edge, to_edge)

        ## Remove the initial 'Home' stage and update the from of the second stage.
        person_stages[1] = person_stages[1]._replace(fromEdge=person_stages[0].fromEdge)
        is_start_to_fix = True
        if person_stages[0].start:
            is_start_to_fix = False
            person_stages[1] = person_stages[1]._replace(start=person_stages[0].stage)
        del person_stages[0]

        ## Fixing the 'from' field with a forward chain
        pos = 2
        while pos in person_stages:
            person_stages[pos] = person_stages[pos]._replace(fromEdge=person_stages[pos-1].toEdge)
            pos += 1

        ## IF NECESSARY, compute the real starting time for the activity chain.
        if is_start_to_fix:
            start = self._stages_compute_start_time(person_stages)
            person_stages[1] = person_stages[1]._replace(start=start)

        ## Define the position of each location in the activity chain.
        person_stages = self._stages_define_locations_position(person_stages)

        return person_stages

    def _random_location_circle(self, center, other):
        """ Return a random edge in within a radius (*) from the given center.

            (*) Uses the ellipses defined by the foci center and other,
                and the major axe of 1.30 * distance between the foci.
        """
        length = None
        try:
            length = traci.simulation.findRoute(center, other).length
        except traci.exceptions.TraCIException:
            raise TripGenerationActivityError('No route between {} and {}'.format(center, other))
        major_axe = length * 1.3
        minor_axe = numpy.sqrt(numpy.square(major_axe) - numpy.square(length))
        radius = minor_axe / 2.0

        edges = self._get_all_neigh_edges(center, radius)
        if not edges:
            raise TripGenerationActivityError(
                'No edges from {} with range {}.'.format(center, length))

        ret = self._random_generator.choice(edges)
        edges.remove(ret)
        allowed = (
            self._sumo_network.getEdge(ret).allows('pedestrian') and
            self._sumo_network.getEdge(ret).allows('passenger') and
            ret != center and ret != other and
            self._sumo_network.getEdge(ret).getLength() > self._conf['minEdgeAllowed'])
        while edges and not allowed:
            ret = self._random_generator.choice(edges)
            edges.remove(ret)
            allowed = (
                self._sumo_network.getEdge(ret).allows('pedestrian') and
                self._sumo_network.getEdge(ret).allows('passenger') and
                ret != center and ret != other and
                self._sumo_network.getEdge(ret).getLength() > self._conf['minEdgeAllowed'])

        if not edges:
            raise TripGenerationActivityError(
                'No valid edges from {} with range {}.'.format(center, length))
        return ret

    def _random_location_ellipse(self, focus1, focus2):
        """ Return a random edge in within the ellipse defined by the foci,
            and the major axe of 1.30 * distance between the foci.
        """
        length = None
        try:
            length = traci.simulation.findRoute(focus1, focus2).length
            logging.debug('%s --> %s : %.2f', focus1, focus2, length)
        except traci.exceptions.TraCIException:
            raise TripGenerationActivityError('No route between {} and {}'.format(focus1, focus2))

        major_axe = length * 1.3

        edges = self._get_all_neigh_edges(focus1, length)
        while edges:
            edge = self._random_generator.choice(edges)
            edges.remove(edge)
            if edge == focus1 or edge == focus2:
                continue
            allowed = (
                self._sumo_network.getEdge(edge).allows('pedestrian') and
                self._sumo_network.getEdge(edge).allows('passenger') and
                self._sumo_network.getEdge(edge).getLength() > self._conf['minEdgeAllowed'])
            if not allowed:
                continue
            try:
                first = traci.simulation.findRoute(focus1, edge).length
                second = traci.simulation.findRoute(edge, focus2).length
                if first + second <= major_axe:
                    logging.debug('%s --> %s : %.2f', focus1, edge, first)
                    logging.debug('%s --> %s : %.2f', edge, focus2, second)
                    return edge
            except traci.exceptions.TraCIException:
                pass

        raise TripGenerationActivityError(
            "No location available for _random_location_ellipse [{}, {}]".format(focus1, focus2))

    def _get_all_neigh_edges(self, origin, distance):
        """ Returns all the edges reachable from the origin within the given radius. """
        _edge_shape = self._sumo_network.getEdge(origin).getShape()
        x_coord = _edge_shape[-1][0]
        y_coord = _edge_shape[-1][1]
        edges = self._sumo_network.getNeighboringEdges(x_coord, y_coord, r=distance)
        edges = [edge.getID() for edge, _ in edges]
        return edges

    def _get_timing_from_activity(self, activity):
        """ Compute start and duration from the activity defined in the config file. """
        start = None
        if self._conf['activities'][activity]['start']:
            start = self._random_generator.normal(
                loc=self._conf['activities'][activity]['start']['m'],
                scale=self._conf['activities'][activity]['start']['s'])
            if start < 0:
                return self._get_timing_from_activity(activity)
        duration = None
        if self._conf['activities'][activity]['duration']:
            duration = self._random_generator.normal(
                loc=self._conf['activities'][activity]['duration']['m'],
                scale=self._conf['activities'][activity]['duration']['s'])
            if duration <= 0:
                return self._get_timing_from_activity(activity)
        return start, duration

    ## ---- PAIR SELECTION: origin - destination - mode ---- ##

    def _select_pair(self, from_area, to_area, pedestrian=False):
        """ Randomly select one pair, chosing between buildings and TAZ. """
        from_taz = str(self._select_taz_from_weighted_area(from_area))
        to_taz = str(self._select_taz_from_weighted_area(to_area))

        if from_taz in self._buildings_by_taz.keys() and to_taz in self._buildings_by_taz.keys():
            return self._select_pair_from_taz_wbuildings(
                self._buildings_by_taz[from_taz][:], self._buildings_by_taz[to_taz][:], pedestrian)
        return self._select_pair_from_taz(
            self._edges_by_taz[from_taz][:], self._edges_by_taz[to_taz][:])

    def _select_taz_from_weighted_area(self, area):
        """ Select a TAZ from an area using its weight. """
        selection = self._random_generator.uniform(0, 1)
        total_weight = sum([self._taz_weights[taz]['weight'] for taz in area])
        cumulative = 0.0
        for taz in area:
            cumulative += self._taz_weights[taz]['weight'] / total_weight
            if selection <= cumulative:
                return taz
        return None # this is matematically impossible,
                    # if this happens, there is a mistake in the weights.

    def _valid_pair(self, from_edge, to_edge):
        """ This is just to avoid a HUGE while condition.
            sumolib.net.edge.is_fringe()
        """
        from_edge_sumo = self._sumo_network.getEdge(from_edge)
        to_edge_sumo = self._sumo_network.getEdge(to_edge)

        if from_edge_sumo.is_fringe(from_edge_sumo.getOutgoing()):
            return False
        if to_edge_sumo.is_fringe(to_edge_sumo.getIncoming()):
            return False
        if from_edge == to_edge:
            return False
        if to_edge in self._blacklisted_edges:
            return False
        if not to_edge_sumo.allows('pedestrian'):
            return False
        return True

    def _select_pair_from_taz(self, from_taz, to_taz):
        """ Randomly select one pair from a TAZ.
            Important: from_taz and to_taz MUST be passed by copy.
            Note: sumonet.getEdge(from_edge).allows(v_type) does not support distributions.
        """

        from_edge = from_taz.pop(
            self._random_generator.randint(0, len(from_taz)))
        to_edge = to_taz.pop(
            self._random_generator.randint(0, len(to_taz)))

        _to = False
        while not self._valid_pair(from_edge, to_edge) and from_taz and to_taz:
            if not self._sumo_network.getEdge(to_edge).allows('pedestrian') or _to:
                to_edge = to_taz.pop(
                    self._random_generator.randint(0, len(to_taz)))
                _to = False
            else:
                from_edge = from_taz.pop(
                    self._random_generator.randint(0, len(from_taz)))
                _to = True

        return from_edge, to_edge

    def _select_pair_from_taz_wbuildings(self, from_buildings, to_buildings, pedestrian):
        """ Randomly select one pair from a TAZ.
            Important: from_buildings and to_buildings MUST be passed by copy.
            Note: sumonet.getEdge(from_edge).allows(v_type) does not support distributions.
        """

        from_edge, _index = self._get_weighted_edge(
            from_buildings, self._random_generator.random_sample(), False)
        del from_buildings[_index]
        to_edge, _index = self._get_weighted_edge(
            to_buildings, self._random_generator.random_sample(), pedestrian)
        del to_buildings[_index]

        _to = True
        while not self._valid_pair(from_edge, to_edge) and from_buildings and to_buildings:
            if not self._sumo_network.getEdge(to_edge).allows('pedestrian') or _to:
                to_edge, _index = self._get_weighted_edge(
                    to_buildings, self._random_generator.random_sample(), pedestrian)
                del to_buildings[_index]
                _to = False
            else:
                from_edge, _index = self._get_weighted_edge(
                    from_buildings, self._random_generator.random_sample(), False)
                del from_buildings[_index]
                _to = True

        return from_edge, to_edge

    @staticmethod
    def _get_weighted_edge(edges, double, pedestrian):
        """ Return an edge and its position using the cumulative sum of the weigths in the area. """
        pos = -1
        ret = None
        for cum_sum, g_edge, p_edge, _ in edges:
            if ret and cum_sum > double:
                return ret, pos
            if pedestrian and p_edge:
                ret = p_edge
            elif not pedestrian and g_edge:
                ret = g_edge
            elif g_edge:
                ret = g_edge
            else:
                ret = p_edge
            pos += 1

        return edges[-1][1], len(edges) - 1


    ## ---- INTERMODAL: modes and route validity ---- ##

    @staticmethod
    def _get_mode_parameters(mode):
        """ Return the correst TraCI parameters for the requested mode.
            Parameters: _mode, _ptype, _vtype
        """
        if mode == 'public':
            return 'public', '', ''
        elif mode == 'bicycle':
            return 'bicycle', '', 'bicycle'
        elif mode == 'walk':
            return '', 'pedestrian', ''
        return '', '', mode     # 'car', '', mode (but car is not really necessary,
                                #                  cause it creates unusable alternatives)

    def _is_valid_route(self, mode, route):
        """ Handle findIntermodalRoute results. """
        if route is None:
            # traci failed
            return False
        _mode, _ptype, _vtype = self._get_mode_parameters(mode)
        if _mode is None:
            # only for findRoute
            if len(route.edges) >= 2:
                return True
        elif _mode == 'public':
            for stage in route:
                if stage.line:
                    return True
        elif mode == 'car':
            for stage in route:
                if stage.type == tc.STAGE_DRIVING and len(stage.edges) >= 2:
                    return True
        else:
            for stage in route:
                if len(stage.edges) >= 2:
                    return True
        return False

    @staticmethod
    def _cost_from_route(route):
        """ Compute the route cost. """
        cost = 0.0
        for stage in route:
            cost += stage.cost
        return cost

    ## ---------------------------------------------------------------------------------------- ##
    ##                                Saving trips to files                                     ##
    ## ---------------------------------------------------------------------------------------- ##

    ROUTES_TPL = """<?xml version="1.0" encoding="UTF-8"?>

<!-- Generated with SUMO Activity-Based Mobility Generator [https://github.com/lcodeca/SUMOActivityGen] -->

<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/routes_file.xsd"> {trips}
</routes>"""

    VEHICLE = """
    <vehicle id="{id}" type="{v_type}" depart="{depart}" departLane="best" arrivalPos="{arrival}">{route}{stop}
    </vehicle>"""

    ROUTE = """
        <route edges="{edges}"/>"""

    STOP_PARKING_TRIGGERED = """
        <stop parkingArea="{id}" triggered="true" expected="{person}"/>"""

    STOP_EDGE_TRIGGERED = """
        <stop lane="{lane}" parking="true" startPos="{start}" endPos="{end}" triggered="true" expected="{person}"/>"""

    ONDEMAND_TRIGGERED = """
        <stop lane="{lane}" parking="true" startPos="{start}" endPos="{end}" duration="1.0"/>"""

    FINAL_STOP = """
        <stop lane="{lane}" duration="1.0"/>"""

    PERSON = """
    <person id="{id}" type="pedestrian" depart="{depart}">{stages}
    </person>"""

    WAIT = """
        <stop lane="{lane}" duration="{duration}" actType="{action}"/>"""

    WALK = """
        <walk edges="{edges}"/>"""

    WALK_W_ARRIVAL = """
        <walk edges="{edges}" arrivalPos="{arrival}"/>"""

    WALK_BUS = """
        <walk edges="{edges}" busStop="{busStop}"/>"""

    RIDE_BUS = """
        <ride busStop="{busStop}" lines="{lines}" intended="{intended}" depart="{depart}"/>"""

    RIDE_TRIGGERED = """
        <ride from="{from_edge}" to="{to_edge}" arrivalPos="{arrival}" lines="{vehicle_id}"/>"""

    VEHICLE_TRIGGERED = """
    <vehicle id="{id}" type="{v_type}" depart="triggered">{route}{stops}
    </vehicle>"""

    VEHICLE_TRIGGERED_DEPART = """
    <vehicle id="{id}" type="{v_type}" depart="triggered" departPos="{depart}">{route}{stops}
    </vehicle>"""

    def _get_stopping_lane(self, edge):
        """ Returns the vehicle-friendly stopping lange closer to the sidewalk. """
        for lane in self._sumo_network.getEdge(edge).getLanes():
            if lane.allows('passenger'):
                return lane.getID()
        raise TripGenerationGenericError("'passenger' cannot stop on edge {}".format(edge))

    def _generate_sumo_trip_from_activitygen(self, person):
        """ Generate the XML string for SUMO route file from a person-trip. """
        complete_trip = ''
        triggered = ''
        _triggered_counter = 0 ## to be used with on-demand vehicles
        _tr_id = '{}_tr'.format(person['id'])
        _triggered_vtype = ''
        _triggered_route = []
        _triggered_stops = ''
        stages = ''
        _last_arrival_pos = None
        _internal_consistency_check = []
        for stage in person['stages']:
            if stage.type == tc.STAGE_WAITING:
                stages += self.WAIT.format(lane=stage.edges,
                                           duration=stage.travelTime,
                                           action=stage.description)
            elif stage.type == tc.STAGE_WALKING:
                if stage.destStop:
                    stages += self.WALK_BUS.format(
                        edges=' '.join(stage.edges), busStop=stage.destStop)
                else:
                    if stage.arrivalPos:
                        stages += self.WALK_W_ARRIVAL.format(
                            edges=' '.join(stage.edges), arrival=stage.arrivalPos)
                        _last_arrival_pos = stage.arrivalPos
                    else:
                        stages += self.WALK.format(edges=' '.join(stage.edges))
            elif stage.type == tc.STAGE_DRIVING:
                if stage.line != stage.intended:
                    # intended is the transport id, so it must be different
                    stages += self.RIDE_BUS.format(
                        busStop=stage.destStop, lines=stage.line,
                        intended=stage.intended, depart=stage.depart)
                else:
                    # triggered vehicle (line = intended)
                    _ride_id = None
                    if stage.intended == 'on-demand':
                        ## generate a new vehicle
                        _triggered_counter += 1 ## I don't want to start from 0
                        _ride_id = '{}_{}_od'.format(person['id'], _triggered_counter)
                        _route = self.ROUTE.format(edges=' '.join(stage.edges))
                        _vtype = stage.vType
                        _stop = ''
                        start = stage.arrivalPos - self._conf['stopBufferDistance'] / 2.0
                        end = stage.arrivalPos + self._conf['stopBufferDistance'] / 2.0
                        _stop = self.ONDEMAND_TRIGGERED.format(
                            lane=self._get_stopping_lane(stage.edges[-1]),
                            start=start, end=end)
                        if _last_arrival_pos:
                            triggered += self.VEHICLE_TRIGGERED_DEPART.format(
                                id=_ride_id, v_type=_vtype, route=_route, stops=_stop,
                                depart=_last_arrival_pos)
                        else:
                            triggered += self.VEHICLE_TRIGGERED.format(
                                id=_ride_id, v_type=_vtype, route=_route, stops=_stop)
                        _last_arrival_pos = stage.arrivalPos
                    else:
                        ## consistency check
                        _internal_consistency_check.append(stage.intended)
                        ## add to the existing one
                        _ride_id = _tr_id
                        if _triggered_route:
                            ## check for contiguity
                            if _triggered_route[-1] != stage.edges[0]:
                                raise TripGenerationInconsistencyError(
                                    'Triggered vehicle has a broken route.')
                            else:
                                ## remove the duplicated edge
                                _triggered_route.extend(stage.edges[1:])
                        else:
                            ## nothing to be "fixed"
                            _triggered_route.extend(stage.edges)
                        _triggered_vtype = stage.vType
                        _stop = ''
                        # print(stage.travelTime, stage.destStop)
                        if stage.travelTime == -42.42:
                            # print('final stop')
                            _stop = self.FINAL_STOP.format(
                                lane=self._get_stopping_lane(stage.edges[-1]))
                        else:
                            if stage.destStop:
                                # print('parking')
                                _stop = self.STOP_PARKING_TRIGGERED.format(
                                    id=stage.destStop, person=person['id'])
                            else:
                                # print('side edge')
                                start = stage.arrivalPos - self._conf['stopBufferDistance'] / 2.0
                                end = stage.arrivalPos + self._conf['stopBufferDistance'] / 2.0
                                _stop = self.STOP_EDGE_TRIGGERED.format(
                                    lane=self._get_stopping_lane(stage.edges[-1]),
                                    person=person['id'], start=start, end=end)
                        _triggered_stops += _stop

                    stages += self.RIDE_TRIGGERED.format(
                        from_edge=stage.edges[0], to_edge=stage.edges[-1], vehicle_id=_ride_id,
                        arrival=stage.arrivalPos)

        ## fixing the personal triggered vehicles
        if _triggered_route:
            _route = self.ROUTE.format(edges=' '.join(_triggered_route))
            triggered += self.VEHICLE_TRIGGERED.format(
                id=_tr_id, v_type=_triggered_vtype, route=_route,
                stops=_triggered_stops, arrival='random', depart='random')

        ## internal consistency test
        if _internal_consistency_check:
            if person['stages'][0].type != tc.STAGE_DRIVING:
                raise TripGenerationInconsistencyError(
                    'Triggered vehicle does not start from the beginning.')
            if person['stages'][-2].type != tc.STAGE_DRIVING:
                ## person['stages'][-1] is the stop
                raise TripGenerationInconsistencyError(
                    'Triggered vehicle does not finish at the end.')

        ## result
        complete_trip += triggered
        complete_trip += self.PERSON.format(
            id=person['id'], depart=person['depart'], stages=stages)
        return complete_trip

    def _saving_trips_to_files(self):
        """ Saving all te trips to files divided by slice. """

        for name, dict_trips in self._all_trips.items():
            filename = '{}/{}{}.rou.xml'.format(BASE_DIR, self._conf['outputPrefix'], name)
            with open(filename, 'w') as tripfile:
                all_trips = ''
                for time in sorted(dict_trips.keys()):
                    for person in dict_trips[time]:
                        all_trips += person['string']

                tripfile.write(self.ROUTES_TPL.format(trips=all_trips))
            logging.info('Saved %s', filename)

def _main():
    """ Person Trip Activity-based Mobility Generation with PoIs and TAZ. """

    args = _args()

    ## ========================              PROFILER              ======================== ##
    if args.profiling:
        profiler = cProfile.Profile()
        profiler.enable()
    ## ========================              PROFILER              ======================== ##

    logging.info('Loading configuration file %s.', args.config)
    conf = _load_configurations(args.config)

    mobility = MobilityGenerator(conf, profiling=args.profiling)
    mobility.mobility_generation()
    mobility.save_mobility()
    mobility.close_traci()

    ## ========================              PROFILER              ======================== ##
    if args.profiling:
        profiler.disable()
        results = io.StringIO()
        pstats.Stats(profiler, stream=results).sort_stats('cumulative').print_stats(25)
        print(results.getvalue())
    ## ========================              PROFILER              ======================== ##

    logging.info('Done.')

if __name__ == "__main__":
    _logs()
    _main()
