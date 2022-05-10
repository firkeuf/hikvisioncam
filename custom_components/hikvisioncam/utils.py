import pyhik.hikvision

import datetime
import logging
import time
from PIL import Image
import io
import threading
import requests


try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

# Make pydispatcher optional to support legacy implentations
# New usage should implement the event_callback
try:
    from pydispatch import dispatcher
except ImportError:
    dispatcher = None

from pyhik.constants import (
    DEFAULT_PORT, DEFAULT_HEADERS, XML_NAMESPACE, SENSOR_MAP,
    CAM_DEVICE, NVR_DEVICE, CONNECT_TIMEOUT, READ_TIMEOUT, CONTEXT_INFO,
    CONTEXT_TRIG, CONTEXT_MOTION, CONTEXT_ALERT, CHANNEL_NAMES, ID_TYPES,
    __version__)

_LOGGING = logging.getLogger(__name__)

REGION_IDS = [1, 2, 3, 4]
REGION_SENSORS = ['Line Crossing',
                  'Entering Region',
                  ]


def box_normalization(box):
    if not box:
        return None
    box_l = list(map(int, box))
    if len(box_l) == 4:
        x0, y0, x1, y1 = box_l
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        return [x0, y0, x1, y1]
    elif len(box_l) == 8:
        x = box_l[0::2]
        y = box_l[1::2]
        x.sort()
        y.sort()
        return [x[0], y[0], x[-1], y[-1]]


class HikCamera(pyhik.hikvision.HikCamera):
    def __init__(self, host=None, port=DEFAULT_PORT,
                 usr=None, pwd=None, verify_ssl=True):
        super(HikCamera, self).__init__(host, port, usr, pwd, verify_ssl)
        self.curent_event_region = {}
        self.current_attr = []

    def alert_stream(self, reset_event, kill_event):
        """Open event stream."""
        _LOGGING.debug('Stream Thread Started: %s, %s', self.name, self.cam_id)
        start_event = False
        parse_string = ""
        fail_count = 0

        url = '%s/ISAPI/Event/notification/alertStream' % self.root_url

        # pylint: disable=too-many-nested-blocks
        while True:
            next_content = False
            raw = io.BytesIO()

            try:
                stream = self.hik_request.get(url, stream=True,
                                              timeout=(CONNECT_TIMEOUT,
                                                       READ_TIMEOUT))
                if stream.status_code == requests.codes.not_found:
                    # Try alternate URL for stream
                    url = '%s/Event/notification/alertStream' % self.root_url
                    stream = self.hik_request.get(url, stream=True)

                if stream.status_code != requests.codes.ok:
                    raise ValueError('Connection unsucessful.')
                else:
                    _LOGGING.debug('%s Connection Successful.', self.name)
                    fail_count = 0
                    self.watchdog.start()

                for line in stream.iter_lines(chunk_size=1):
                    # _LOGGING.debug('Processing line from %s', self.name)
                    # filter out keep-alive new lines
                    if line:
                        str_line = line.decode("utf-8", "ignore")

                        if 'image/jpeg' in str_line:
                            next_content = True
                            continue

                        if next_content:
                            try:
                                content_length = int(line.decode().split(' ')[1])
                            except Exception as e:
                                _LOGGING.error(f'Can not parse content length {e}')

                            next_content = False
                            time_stamp = self._sensor_last_tripped_time()
                            try:
                                box = self.current_attr[5]
                            except Exception:
                                box = False
                            path = self.current_attr[7] #self._sensor_image_path(self.name, box, time_stamp, self.current_attr[6], self.current_attr[4])
                            with open(path, 'wb') as f:
                                chunk = stream.raw.read(content_length+3)  # remove \n\r\n
                                fixed_chunk = chunk.removeprefix(b'\n\r\n')  # remove \n\r\n
                                f.write(fixed_chunk)
                            raw = io.BytesIO(fixed_chunk)
                            with Image.open(raw) as img:
                                box = self.current_attr[5]
                                if box:
                                    width, height = img.size
                                    left = width * box[0] #*100/100
                                    top = height * box [1]
                                    right = width * (box[0] + box [2])
                                    bottom = height * (box[1] + box[3])
                                    object = (left, top, right, bottom)
                                    try:
                                        img_crop = img.crop(object)
                                        img_crop.save(f'{path.removesuffix("jpg")}crop.jpg')

                                    except Exception as ee:
                                        _LOGGING.info(f'_get_image EXCEPTION 0 {ee}')
                                else:
                                    pass
                            continue
                        # New events start with --boundry
                        if str_line.find('<EventNotificationAlert') != -1:
                            # Start of event message
                            start_event = True
                            parse_string = str_line
                        elif str_line.find('</EventNotificationAlert>') != -1:
                            # Message end found found
                            parse_string += str_line
                            start_event = False
                            if parse_string:
                                try:
                                    tree = ET.fromstring(parse_string)
                                    self.process_stream(tree)
                                    self.update_stale()
                                except ET.ParseError as err:
                                    _LOGGING.warning('XML parse error in stream.')
                                parse_string = ""
                        else:
                            if start_event:
                                parse_string += str_line

                    if kill_event.is_set():
                        # We were asked to stop the thread so lets do so.
                        break
                    elif reset_event.is_set():
                        # We need to reset the connection.
                        raise ValueError('Watchdog failed.')

                if kill_event.is_set():
                    # We were asked to stop the thread so lets do so.
                    _LOGGING.debug('Stopping event stream thread for %s',
                                   self.name)
                    self.watchdog.stop()
                    self.hik_request.close()
                    return
                elif reset_event.is_set():
                    # We need to reset the connection.
                    raise ValueError('Watchdog failed.')

            except (ValueError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as err:
                fail_count += 1
                reset_event.clear()
                _LOGGING.warning('%s Connection Failed (count=%d). Waiting %ss. Err: %s',
                                 self.name, fail_count, (fail_count * 5) + 5, err)
                parse_string = ""
                self.watchdog.stop()
                self.hik_request.close()
                time.sleep(5)
                self.update_stale()
                time.sleep(fail_count * 5)
                continue


    def process_stream(self, tree):
        """Process incoming event stream packets."""
        if not self.namespace[CONTEXT_ALERT]:
            self.fetch_namespace(tree, CONTEXT_ALERT)

        try:
            etype = SENSOR_MAP[tree.find(
                self.element_query('eventType', CONTEXT_ALERT)).text.lower()]

            # Since this pasing is different and not really usefull for now, just return without error.
            if len(etype) > 0 and etype == 'Ongoing Events':
                return

            estate = tree.find(
                self.element_query('eventState', CONTEXT_ALERT)).text

            for idtype in ID_TYPES:
                echid = tree.find(self.element_query(idtype, CONTEXT_ALERT))
                if echid is not None:
                    try:
                        # Need to make sure this is actually a number
                        echid = int(echid.text)
                        break
                    except (ValueError, TypeError) as err:
                        # Field must not be an integer or is blank
                        pass

            ecount = tree.find(
                self.element_query('activePostCount', CONTEXT_ALERT)).text
            try:
                region_id = tree.find(f"{self.element_query('DetectionRegionList', CONTEXT_ALERT)}"
                                      f"/{self.element_query('DetectionRegionEntry', CONTEXT_ALERT)}"
                                      f"/{self.element_query('regionID', CONTEXT_ALERT)}").text
                #box_tree = tree.find(f"{self.element_query('DetectionRegionList', CONTEXT_ALERT)}"
                #                     f"/{self.element_query('DetectionRegionEntry', CONTEXT_ALERT)}"
                #                     f"/{self.element_query('RegionCoordinatesList', CONTEXT_ALERT)}")


                detectionTarget = tree.find(f"{self.element_query('DetectionRegionList', CONTEXT_ALERT)}"
                                            f"/{self.element_query('DetectionRegionEntry', CONTEXT_ALERT)}"
                                            f"/{self.element_query('detectionTarget', CONTEXT_ALERT)}").text

                box_tree = tree.find(f"{self.element_query('DetectionRegionList', CONTEXT_ALERT)}"
                                                   f"/{self.element_query('DetectionRegionEntry', CONTEXT_ALERT)}"
                                                   f"/{self.element_query('TargetRect', CONTEXT_ALERT)}")

                box = [float(q.text) for q in box_tree.iter() if not q]
            except:
                region_id = ''
                box = []
                detectionTarget = 'others'
        except (AttributeError, KeyError, IndexError) as err:
            _LOGGING.error('Problem finding attribute: %s', err)
            return

        # Take care of keep-alive
        if len(etype) > 0 and etype == 'Video Loss':
            self.watchdog.pet()

        # Track state if it's in the event list.
        if len(etype) > 0:
            state = self.fetch_attributes(etype, echid)
            if state:
                # Determine if state has changed
                # If so, publish, otherwise do nothing
                estate = (estate == 'active')
                old_state = state[0]
                eventTime = datetime.datetime.now()
                path = self._sensor_image_path(self.name, box, eventTime.timestamp(), etype, region_id)
                attr = [estate, echid, int(ecount),
                        eventTime,
                        region_id, box, detectionTarget, path]
                self.current_attr = attr
                #self.update_attributes(etype, echid, attr)
                if estate:
                    self.curent_event_region.update({etype: region_id})
                if True:  # estate != old_state:
                    if not region_id:
                        region_id = self.curent_event_region.get(etype, '')

                    if etype in REGION_SENSORS and not estate:
                        for r in REGION_IDS:
                            self.publish_changes(etype, echid, str(r), estate, attr)
                    else:
                        self.publish_changes(etype, echid, region_id, estate, attr)
                self.watchdog.pet()

    def _sensor_last_tripped_time(self):
        """Extract sensor last update time."""
        try:
            attr = self.current_attr  #self._cam.get_attributes(self._sensor, self._channel)
            time_stamp = attr[3].timestamp()
        except Exception as e:
            _LOGGING.warning(f'_sensor_last_tripped_time Except {e}')
            return time.time()
        return time_stamp

    def _sensor_image_path(self, name, box, time_stamp, etype, region):
        #if not self.is_on:
        #    return ''
        if box:
            filename = f'/config/www/hikvision/image_{name}_{time_stamp}_{etype}_{region}_{box[0]}_{box[1]}_{box[2]}_{box[3]}.jpg'
        else:
            filename = f'/config/www/hikvision/image_{name}_{time_stamp}_{etype}_{region}_full.jpg'
        return filename

    def get_image(self, box, path):
        t = threading.Thread(target=self._get_image, args=(box, path,), name='GetImage')
        t.start()

    def _get_image(self, box, path):
        url = '%s/ISAPI/Streaming/channels/101/picture'
        try:
            response = self.hik_request.get(url % self.root_url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True)
            raw = io.BytesIO(response.content)
            try:
                with Image.open(raw) as img:
                    if box:
                        try:
                            img_crop = img.crop(box)
                            img_crop.save(path)
                            img.save(f'{path}.orig.jpg')
                        except Exception as ee:
                            _LOGGING.info(f'_get_image EXCEPTION 0 {ee}')
                    else:
                        img.save(path)
            except Exception as eee:
                _LOGGING.info(f'_get_image EXCEPTION eee {eee}')

        except Exception as e:
            _LOGGING.info(f'_get_image EXCEPTION {e}')

    def update_attributes(self, event, channel, attr):
        """Update attribute list for current event/channel."""
        try:
            for i, sensor in enumerate(self.event_states[event]):
                if sensor[1] == int(channel):
                    self.event_states[event][i] = attr
        except KeyError:
            _LOGGING.debug('Error updating attributes for: (%s, %s)',
                           event, channel)

    def update_stale(self):
        """Update stale active statuses"""
        # Some events don't post an inactive XML, only active.
        # If we don't get an active update for 5 seconds we can
        # assume the event is no longer active and update accordingly.
        for etype, echannels in self.event_states.items():
            for eprop in echannels:
                if eprop[3] is not None:
                    sec_elap = ((datetime.datetime.now()-eprop[3])
                                .total_seconds())
                    # print('Seconds since last update: {}'.format(sec_elap))
                    if sec_elap > 5 and eprop[0] is True:
                        _LOGGING.debug('Updating stale event %s on CH(%s)',
                                       etype, eprop[1])
                        attr = [False, eprop[1], eprop[2],
                                datetime.datetime.now()]
                        self.update_attributes(etype, eprop[1], attr)
                        self.publish_changes(etype, eprop[1], estate=False, attr=attr)

    def publish_changes(self, etype, echid, region='', estate=None, attr=None):
        """Post updates for specified event type."""
        _LOGGING.warning('%s Update: %s, %s',
                         self.name, etype, self.fetch_attributes(etype, echid))
        signal = 'ValueChanged.{}'.format(self.cam_id)
        sender = '{}.{}'.format(etype, echid)
        if dispatcher:
            dispatcher.send(signal=signal, sender=sender)

        self._do_update_callback(f'{self.cam_id}.{etype}.{echid}{region}', region, estate, attr)

    def _do_update_callback(self, msg, region='', estate=None, attr=None):
        """Call registered callback functions."""
        for callback, sensor in self._updateCallbacks:
            if sensor == msg:
                _LOGGING.debug('Update callback %s for sensor %s',
                               callback, sensor)
                callback(msg, region, estate, attr)
