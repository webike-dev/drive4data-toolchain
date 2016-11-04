import contextlib
import csv
import logging
import os
import pickle

from webike.util.Logging import BraceMessage as __

from util.InfluxDB import InfluxDBStreamingClient as InfluxDBClient

__author__ = "Niko Fink"
logger = logging.getLogger(__name__)

SAVE_FILE = "tmp/counts.pickle"
OUT_FILE = "out/counts.csv"
TIME_EPOCH = 's'
FIELDNAMES = ['time', 'key', 'first', 'last', 'duration', 'min_soc', 'max_soc', 'count_ac_hvpower',
              'count_boardtemperature', 'count_charger_accurrent', 'count_charger_acvoltage', 'count_chargerplugstatus',
              'count_chargetimeremaining', 'count_engine_afr', 'count_engine_rpm', 'count_ev_range_remaining',
              'count_fuel_rate', 'count_gps_alt_metres', 'count_gps_geohash', 'count_gps_lat_deg', 'count_gps_lon_deg',
              'count_gps_speed_kph', 'count_gps_time', 'count_hvbatt_current', 'count_hvbatt_soc', 'count_hvbatt_temp',
              'count_hvbatt_voltage', 'count_hvbs_cors_crnt', 'count_hvbs_fn_crnt', 'count_inputvoltage',
              'count_ischarging', 'count_maf', 'count_motorvoltages', 'count_outside_air_temp', 'count_reltime',
              'count_source', 'count_veh_odometer', 'count_veh_speed', 'count_vin_1', 'count_vin_2', 'count_vin_3',
              'count_vin_digit', 'count_vin_frame1', 'count_vin_frame2', 'count_vin_index']


def extract_res(res, data, func):
    for (meas, groups), iter in res.items():
        d_key = ",".join("{}={}".format(k, v) for k, v in groups.items())
        row = next(iter)
        assert next(iter, None) is None
        if d_key not in data:
            data[d_key] = {}
        key, value = func(row)
        data[d_key][key] = value


def analyze(cred):
    if os.path.isfile(SAVE_FILE):
        data = pickle.load(open(SAVE_FILE, "rb"))
    else:
        data = {}
        with contextlib.closing(InfluxDBClient(cred['host'], cred['port'], cred['user'], cred['passwd'], cred['db'],
                                               time_epoch=TIME_EPOCH)) as client:
            logger.info(__("Querying res_first"))
            res_first = client.query(
                "SELECT participant, car_id, first(source) FROM samples GROUP BY participant, car_id")
            extract_res(res_first, data, lambda row: ('first', row['time']))

            logger.info(__("Querying res_last"))
            res_last = client.query(
                "SELECT participant, car_id, last(source) FROM samples GROUP BY participant, car_id")
            extract_res(res_last, data, lambda row: ('last', row['time']))

            logger.info(__("Querying res_count"))
            res_count = client.query("SELECT count(*) FROM samples GROUP BY participant, car_id")
            extract_res(res_count, data, lambda row: ('counts', row))

            logger.info(__("Querying res_range"))
            res_range = client.query("SELECT min(hvbatt_soc) AS min_soc, max(hvbatt_soc) AS max_soc "
                                     "FROM samples WHERE hvbatt_soc < 200 GROUP BY participant, car_id")
            extract_res(res_range, data, lambda row: ('min_soc', row['min_soc']))
            extract_res(res_range, data, lambda row: ('max_soc', row['max_soc']))

        pickle.dump(data, open(SAVE_FILE, "wb"))

    return ({
                'key': k,
                'first': v.get('first') / 86400 + 25569,  # to libreoffice timestamp
                'last': v.get('last') / 86400 + 25569,
                'duration': (v.get('last') - v.get('first')) / 86400,
                'min_soc': v.get('min_soc'),
                'max_soc': v.get('max_soc'),
                **v.get('counts', {})
            } for k, v in data.items())


def dump(rows, to_file, fieldnames=FIELDNAMES):
    writer = csv.DictWriter(to_file, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)