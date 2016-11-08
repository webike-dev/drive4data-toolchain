import copy
import csv
import logging
import math
from datetime import datetime
from datetime import timedelta

import webike
from drive4data.data.activity import InfluxActivityDetection, ValueMemoryMixin, ValueMemory
from drive4data.data.soc import SoCMixin
from iss4e.db.influxdb import InfluxDBStreamingClient as InfluxDBClient
from iss4e.db.influxdb import TO_SECONDS
from iss4e.util import BraceMessage as __
from iss4e.util import progress
from webike.data import Trips
from webike.util.activity import Cycle
from webike.util.plot import to_hour_bin

__author__ = "Niko Fink"
logger = logging.getLogger(__name__)


def get_current(sample):
    current = None
    if sample.get('hvbatt_current') is not None:
        current = sample['hvbatt_current']
    elif sample.get('hvbs_fn_crnt') is not None and -23 < sample['hvbs_fn_crnt'] < 22:
        current = sample['hvbs_fn_crnt']
    elif sample.get('hvbs_cors_crnt') is not None:
        current = sample['hvbs_cors_crnt']
    return current


class TripDetection(ValueMemoryMixin, SoCMixin, InfluxActivityDetection):
    MIN_DURATION = timedelta(minutes=10) / timedelta(seconds=1)

    def __init__(self, **kwargs):
        # save these values and store the respective first and last value with each cycle
        memorized_values = [
            ValueMemory('veh_odometer', save_first='odo_start', save_last='odo_end'),
            ValueMemory('outside_air_temp', save_last='temp_last')]
        super().__init__(attr='veh_speed',
                         min_sample_count=60, min_cycle_duration=timedelta(minutes=1),
                         max_merge_gap=timedelta(minutes=10),
                         memorized_values=memorized_values, **kwargs)

    def is_start(self, sample, previous):
        return sample[self.attr] > 0.1

    def is_end(self, sample, previous):
        return sample[self.attr] < 0.1 or self.get_duration(previous, sample) > self.MIN_DURATION

    def accumulate_samples(self, new_sample, accumulator):
        accumulator = super().accumulate_samples(new_sample, accumulator)

        # accumulated values depending on previous sample
        if 'est_distance' not in accumulator:
            accumulator['est_distance'] = 0.0
        if '__prev' in accumulator:
            interval = self.get_duration(accumulator['__prev'], new_sample)

            # distance
            distance = (interval / TO_SECONDS['h']) * new_sample['veh_speed']
            accumulator['est_distance'] += distance

        # average values
        self.make_avg(accumulator, 'avg_current', get_current(new_sample))
        self.make_avg(accumulator, 'avg_voltage', new_sample.get('hvbatt_voltage'))
        self.make_avg(accumulator, 'avg_fuel_rate', new_sample.get('fuel_rate'))
        self.make_avg(accumulator, 'temp_avg', new_sample.get('outside_air_temp'))

        accumulator['__prev'] = new_sample
        return accumulator

    def make_avg(self, accumulator, name, value):
        if value is not None and math.isfinite(value):
            if name in accumulator:
                accumulator[name] = (accumulator[name] + value) / 2
            else:
                accumulator[name] = float(value)

    def merge_stats(self, stats1, stats2):
        stats = super().merge_stats(stats1, stats2)

        stats['est_distance'] = stats1['est_distance'] + stats2['est_distance']
        self.merge_avg('avg_current', stats, stats1, stats2)
        self.merge_avg('avg_voltage', stats, stats1, stats2)
        self.merge_avg('avg_fuel_rate', stats, stats1, stats2)
        self.merge_avg('temp_avg', stats, stats1, stats2)

        return stats

    @staticmethod
    def merge_avg(name, stats, stats1, stats2):
        if stats1.get(name) and stats2.get(name):
            stats[name] = (stats1[name] + stats2[name]) / 2
        elif stats1.get(name):
            stats[name] = stats1[name]
        elif stats2.get(name):
            stats[name] = stats2[name]

    def cycle_to_events(self, cycle: Cycle, measurement):
        for event in super().cycle_to_events(cycle, measurement):
            for key in 'est_distance', 'avg_current', 'avg_voltage', 'avg_fuel_rate', 'temp_avg':
                if key in cycle.stats and math.isfinite(cycle.stats[key]):
                    event['fields'][key] = float(cycle.stats[key])
            yield event


class TripDetectionHistogram(TripDetection):
    def __init__(self, **kwargs):
        self.hist_metrics_odo = []
        self.hist_metrics_soc = []
        self.hist_metrics_temp = []
        self.hist_distance = []
        super().__init__(**kwargs)

    def cycle_to_events(self, cycle: Cycle, measurement):
        metrics_odo = cycle.stats['memorized_values']["veh_odometer"]
        self.hist_metrics_odo.append(metrics_odo.get_time_gap(cycle, self.get_duration, missing_value="X"))
        metrics_soc = cycle.stats["soc"]
        self.hist_metrics_soc.append(metrics_soc.get_time_gap(cycle, self.get_duration, missing_value="X"))
        metrics_temp = cycle.stats['memorized_values']["outside_air_temp"]
        self.hist_metrics_temp.append(metrics_temp.get_time_gap(cycle, self.get_duration, missing_value="X"))

        if metrics_odo.first_value() and metrics_odo.last_value():
            self.hist_distance.append(
                (metrics_odo.first_value(), metrics_odo.last_value(), cycle.stats['est_distance']))

        return super().cycle_to_events(cycle, measurement)


def preprocess_trips(client):
    logger.info("Preprocessing trips")
    detector = TripDetectionHistogram(time_epoch=client.time_epoch)
    res = client.stream_series(
        "samples",
        fields="time, veh_speed, participant, veh_odometer, hvbatt_soc, outside_air_temp, "
               "fuel_rate, hvbatt_current, hvbatt_voltage, hvbs_cors_crnt, hvbs_fn_crnt",
        batch_size=500000,
        where="veh_speed > 0")
    for nr, (series, iter) in enumerate(res):
        logger.info(__("#{}: {}", nr, series))
        cycles_curr, cycles_curr_disc = detector(progress(iter))

        logger.info(__("Writing {} + {} = {} trips", len(cycles_curr), len(cycles_curr_disc),
                       len(cycles_curr) + len(cycles_curr_disc)))
        client.write_points(
            detector.cycles_to_timeseries(cycles_curr + cycles_curr_disc, "trips"),
            tags={'detector': detector.attr},
            time_precision=client.time_epoch)

        for name, hist in [('odo', detector.hist_metrics_odo), ('soc', detector.hist_metrics_soc),
                           ('temp', detector.hist_metrics_temp), ('dist', detector.hist_distance)]:
            with open('out/hist_{}_{}.csv'.format(name, nr), 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerows(hist)

        detector.hist_metrics_odo = []
        detector.hist_metrics_soc = []
        detector.hist_metrics_temp = []
        detector.hist_distance = []


def extract_hist(client: InfluxDBClient):
    logger.info("Generating trip histogram data")
    hist_data = copy.deepcopy(webike.data.Trips.HIST_DATA)
    res = client.stream_series("trips", where="discarded !~ /./ AND started = 'True'")
    for nr, (series, iter) in enumerate(res):
        logger.info(__("#{}: {}", nr, series))

        for trip in progress(iter):
            trip_time = datetime.fromtimestamp(trip['time'] * TO_SECONDS[client.time_epoch])
            hist_data['start_times'].append(to_hour_bin(trip_time))
            hist_data['start_weekday'].append(trip_time.weekday())
            hist_data['start_month'].append(trip_time.month)
            hist_data['durations'].append(round(trip['duration'] * TO_SECONDS['n'] / 60))

            hist_trip_mappings = \
                [('distances', 'est_distance'), ('trip_temp', 'outside_air_temp'), ('initial_soc', 'soc_start'),
                 ('final_soc', 'soc_end')]
            for hist_key, trip_key in hist_trip_mappings:
                if trip_key in trip and trip[trip_key] is not None:
                    hist_data[hist_key].append(trip[trip_key])

    return hist_data
