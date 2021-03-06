import logging
from concurrent.futures import Executor
from datetime import timedelta
from multiprocessing.managers import SyncManager

from drive4data.data.activity import InfluxActivityDetection
from drive4data.data.soc import SoCMixin
from iss4e.db.influxdb import InfluxDBStreamingClient as InfluxDBClient, join_selectors
from iss4e.db.influxdb import TO_SECONDS
from iss4e.util import BraceMessage as __, async_progress, progress
from iss4e.util.math import differentiate, smooth
from tabulate import tabulate
from webike.util.activity import Cycle

__author__ = "Niko Fink"
logger = logging.getLogger(__name__)


class ChargeCycleDetection(SoCMixin, InfluxActivityDetection):
    def __init__(self, **kwargs):
        self.last_movement = 0
        self.max_delay = kwargs.pop('max_delay', timedelta(hours=1) / timedelta(seconds=1))
        kwargs.setdefault('max_merge_gap', timedelta(minutes=30))
        kwargs.setdefault('min_cycle_duration', timedelta(minutes=10))
        super().__init__(**kwargs)

    def check_movement(self, sample):
        has_movement = sample.get('veh_speed') and sample['veh_speed'] > 0
        if has_movement:
            self.last_movement = sample['time']
        sample['last_movement'] = self.last_movement
        return has_movement

    def is_start(self, sample, previous):
        return not self.check_movement(sample)

    def is_end(self, sample, previous):
        # we could also detect the end of charging when the SoC doesn't increase for a long time,
        # but that would be a lot of work and is not required right now
        return self.check_movement(sample) or self.get_duration(previous, sample) > self.max_delay

    def check_reject_reason(self, cycle: Cycle):
        assert cycle.start['last_movement'] == cycle.end['last_movement'], "Movement during cycle {}".format(cycle)
        if (cycle.end['hvbatt_soc'] - cycle.start['hvbatt_soc']) < 10:
            return "delta_soc<10%"
        return super().check_reject_reason(cycle)

    def can_merge(self, last_cycle, new_cycle: Cycle):
        if super().can_merge(last_cycle, new_cycle):
            soc_change = new_cycle.start['hvbatt_soc'] - last_cycle.end['hvbatt_soc']
            if soc_change < -2:
                return False
            assert last_cycle.start['last_movement'] == last_cycle.end['last_movement']
            assert new_cycle.start['last_movement'] == new_cycle.end['last_movement']
            if new_cycle.start['last_movement'] > last_cycle.end['time']:
                # vehicle moved between the cycles, don't merge
                return False

            return True
        else:
            return False

    def cycle_to_events(self, cycle: Cycle, measurement=""):
        for cycle, event in zip([cycle.start, cycle.end], super().cycle_to_events(cycle, measurement)):
            event['fields']['last_movement'] = cycle['last_movement']
            yield event


class ChargeCycleDerivDetection(ChargeCycleDetection):
    def __init__(self, **kwargs):
        kwargs.setdefault('max_merge_gap', timedelta(hours=2))
        super().__init__(attr='soc_diff', **kwargs)

    def __call__(self, cycle_samples):
        cycle_samples = smooth(cycle_samples, 'hvbatt_soc', 'soc_smooth', alpha=0.95)
        cycle_samples = differentiate(cycle_samples, 'soc_smooth', 'soc_diff', attr_time='time',
                                      delta_time=TO_SECONDS['h'] / TO_SECONDS['n'])
        return super().__call__(cycle_samples)

    def is_start(self, sample, previous):
        return super().is_start(sample, previous) and sample[self.attr] > 5

    def is_end(self, sample, previous):
        return super().is_end(sample, previous) or sample[self.attr] < 5


class ChargeCycleACVoltageDetection(ChargeCycleDetection):
    def __init__(self, **kwargs):
        super().__init__(attr='charger_acvoltage', **kwargs)

    def is_start(self, sample, previous):
        return super().is_start(sample, previous) and sample[self.attr] > 4

    def is_end(self, sample, previous):
        return super().is_end(sample, previous) or sample[self.attr] < 4


class ChargeCycleIsChargingDetection(ChargeCycleDetection):
    def __init__(self, **kwargs):
        super().__init__(attr='ischarging', **kwargs)

    def is_start(self, sample, previous):
        return super().is_start(sample, previous) and sample[self.attr] > 3

    def is_end(self, sample, previous):
        return super().is_end(sample, previous) or sample[self.attr] < 3


class ChargeCycleACHVPowerDetection(ChargeCycleDetection):
    def __init__(self, **kwargs):
        super().__init__(attr='ac_hvpower', **kwargs)

    def is_start(self, sample, previous):
        return super().is_start(sample, previous) and sample[self.attr] > 0

    def is_end(self, sample, previous):
        return super().is_end(sample, previous) or sample[self.attr] <= 0


def preprocess_cycles(client: InfluxDBClient, executor: Executor, manager: SyncManager, dry_run=False):
    logger.info("Preprocessing charge cycles")
    queue = manager.Queue()
    series = client.list_series("samples")
    futures = []
    # TODO merge results of different detectors
    for attr, where, detector in [
        ('charger_acvoltage', 'charger_acvoltage>0 OR veh_speed > 0',
         ChargeCycleACVoltageDetection(time_epoch=client.time_epoch)),
        ('ischarging', 'ischarging>0 OR veh_speed > 0',
         ChargeCycleIsChargingDetection(time_epoch=client.time_epoch)),
        ('ac_hvpower', 'ac_hvpower>0 OR veh_speed > 0',
         ChargeCycleACHVPowerDetection(time_epoch=client.time_epoch)),
        ('hvbatt_soc', 'hvbatt_soc<200', ChargeCycleDerivDetection(time_epoch=client.time_epoch))
    ]:
        fields = ["time", "participant", "hvbatt_soc", "veh_speed"]
        if attr not in fields:
            fields.append(attr)
        futures += [executor.submit(preprocess_cycle,
                                    nr, client, queue, sname, join_selectors([sselector, where]),
                                    fields, detector, dry_run)
                    for nr, (sname, sselector) in enumerate(series)]

    logger.debug("Tasks started, waiting for results...")
    async_progress(futures, queue)
    data = [f.result() for f in futures]
    logger.debug("Tasks done")
    data.sort(key=lambda a: a[0:1])
    logger.info(__("Detected charge cycles:\n{}", tabulate(data, headers=["attr", "#", "cycles", "cycles_disc"])))


def preprocess_cycle(nr, client, queue, sname, selector, fields, detector, dry_run=False):
    logger.info(__("Processing #{}: {} {}", nr, detector.attr, sname))
    stream = client.stream_params("samples", fields=fields, where=selector, group_order_by="ORDER BY time ASC")
    stream = progress(stream, delay=4, remote=queue.put)
    cycles, cycles_disc = detector(stream)

    if not dry_run:
        logger.info(__("Writing {} + {} = {} cycles", len(cycles), len(cycles_disc),
                       len(cycles) + len(cycles_disc)))
        client.write_points(
            detector.cycles_to_timeseries(cycles + cycles_disc, "charge_cycles"),
            tags={'detector': detector.attr},
            time_precision=client.time_epoch)

    logger.info(__("Task #{}: {} {} completed", nr, detector.attr, sname))
    return detector.attr, nr, len(cycles), len(cycles_disc)
