import logging
import math
from concurrent.futures import Executor
from datetime import timedelta
from multiprocessing.managers import SyncManager

from tabulate import tabulate

from drive4data.data.activity import InfluxActivityDetection, ValueMemory, ValueMemoryMixin
from drive4data.data.soc import SoCMixin
from iss4e.db.influxdb import InfluxDBStreamingClient as InfluxDBClient, TO_SECONDS, join_selectors
from iss4e.util import BraceMessage as __, async_progress, progress
from webike.util.activity import Cycle

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
                         max_merge_gap=timedelta(minutes=4, seconds=20),
                         memorized_values=memorized_values, **kwargs)

    def is_start(self, sample, previous):
        return sample[self.attr] > 0.1

    def is_end(self, sample, previous):
        return sample[self.attr] < 0.1 or self.get_duration(previous, sample) > self.MIN_DURATION

    def accumulate_samples(self, new_sample, accumulator):
        accumulator = super().accumulate_samples(new_sample, accumulator)

        if '__first' not in accumulator:
            accumulator['__first'] = new_sample

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

        # only count temperature 5 mins after trip start
        if self.get_duration(accumulator['__first'], new_sample) >= 5 * TO_SECONDS['m'] \
                and new_sample.get('outside_air_temp') is not None \
                and new_sample.get('outside_air_temp') < 1e305:
            self.make_avg(accumulator, 'temp_avg', new_sample.get('outside_air_temp'))

        accumulator['__prev'] = new_sample
        return accumulator

    def make_avg(self, accumulator, name, value):
        if value is not None and math.isfinite(value):
            cnt = accumulator.get(name + '_cnt', 0) + 1
            accumulator[name] = float(value + (cnt - 1) * accumulator.get(name, 0)) / cnt
            accumulator[name + '_cnt'] = cnt

    def store_cycle(self, cycle: Cycle):
        duration = (cycle.end['time'] - cycle.start['time']) * TO_SECONDS[self.epoch]
        if 'avg_fuel_rate' in cycle.stats:
            cycle.stats['cons_gasoline'] = cycle.stats['avg_fuel_rate'] * duration
        if 'avg_current' in cycle.stats and 'avg_voltage' in cycle.stats:
            cycle.stats['cons_energy'] = cycle.stats['avg_current'] * cycle.stats['avg_voltage'] \
                                         * duration / TO_SECONDS['h']  # convert to Wh
        super().store_cycle(cycle)

    def merge_stats(self, stats1, stats2):
        stats = super().merge_stats(stats1, stats2)

        stats['est_distance'] = stats1['est_distance'] + stats2['est_distance']
        self.merge_avg('avg_current', stats, stats1, stats2)
        self.merge_avg('avg_voltage', stats, stats1, stats2)
        self.merge_avg('avg_fuel_rate', stats, stats1, stats2)
        self.merge_avg('temp_avg', stats, stats1, stats2)
        if 'cons_gasoline' in stats1 or 'cons_gasoline' in stats2:
            stats['cons_gasoline'] = stats1.get('cons_gasoline', 0) + stats2.get('cons_gasoline', 0)
        if 'cons_energy' in stats1 or 'cons_energy' in stats2:
            stats['cons_energy'] = stats1.get('cons_energy', 0) + stats2.get('cons_energy', 0)

        return stats

    @staticmethod
    def merge_avg(name, stats, stats1, stats2):
        if name in stats1 and name in stats2:
            cnt1, cnt2 = stats1.get('cnt', 0), stats2.get('cnt', 0)
            stats[name] = (stats1[name] * cnt1 + stats2[name] * cnt2) / (cnt1 + cnt2)
        elif name in stats1:
            stats[name] = stats1[name]
        elif name in stats2:
            stats[name] = stats2[name]

    def cycle_to_events(self, cycle: Cycle, measurement=""):
        for event in super().cycle_to_events(cycle, measurement):
            for key in 'est_distance', 'avg_current', 'avg_voltage', 'avg_fuel_rate', 'temp_avg', 'cons_gasoline', \
                       'cons_energy':
                if key in cycle.stats and math.isfinite(cycle.stats[key]):
                    event['fields'][key] = float(cycle.stats[key])
            yield event


def preprocess_trips(client: InfluxDBClient, executor: Executor, manager: SyncManager, dry_run=False):
    logger.info("Preprocessing trips")
    queue = manager.Queue()
    series = client.list_series("samples")
    futures = [executor.submit(preprocess_trip, nr, client, queue, sname, sselector, dry_run)
               for nr, (sname, sselector) in enumerate(series)]
    logger.debug("Tasks started, waiting for results...")
    async_progress(futures, queue)
    data = [f.result() for f in futures]
    logger.debug("Tasks done")
    data.sort(key=lambda a: a[0])
    logger.info(__("Detected trips:\n{}", tabulate(data, headers=["#", "cycles", "cycles_disc"])))


def preprocess_trip(nr, client, queue, sname, sselector, dry_run=False):
    logger.info(__("Processing #{}: {}", nr, sname))
    detector = TripDetection(time_epoch=client.time_epoch)
    stream = client.stream_params(
        "samples",
        fields="time, veh_speed, participant, veh_odometer, hvbatt_soc, outside_air_temp, "
               "fuel_rate, hvbatt_current, hvbatt_voltage, hvbs_cors_crnt, hvbs_fn_crnt",
        where=join_selectors([sselector, "veh_speed > 0"]), group_order_by="ORDER BY time ASC"
    )
    stream = progress(stream, delay=4, remote=queue.put)
    cycles, cycles_disc = detector(stream)

    if not dry_run:
        logger.info(__("Writing {} + {} = {} trips", len(cycles), len(cycles_disc),
                       len(cycles) + len(cycles_disc)))
        client.write_points(
            detector.cycles_to_timeseries(cycles + cycles_disc, "trips"),
            tags={'detector': detector.attr},
            time_precision=client.time_epoch)

    logger.info(__("Task #{}: {} completed", nr, sname))
    return nr, len(cycles), len(cycles_disc)
