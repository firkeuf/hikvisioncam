import pyhik.hikvision

import datetime
import logging
import time
from PIL import Image
import io
import threading


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
                box_tree = tree.find(f"{self.element_query('DetectionRegionList', CONTEXT_ALERT)}"
                                     f"/{self.element_query('DetectionRegionEntry', CONTEXT_ALERT)}"
                                     f"/{self.element_query('RegionCoordinatesList', CONTEXT_ALERT)}")
                box = [q.text for q in box_tree.iter() if not q]
            except:
                region_id = ''
                box = []
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
                attr = [estate, echid, int(ecount),
                        datetime.datetime.now(),
                        region_id, box]
                self.update_attributes(etype, echid, attr)

                if estate != old_state:
                    self.publish_changes(etype, echid, region_id)
                self.watchdog.pet()

    def get_image(self, box, path):
        t = threading.Thread(target=self._get_image, args=(box, path,), name='GetImage')
        t.start()

    def _get_image(self, box, path):
        url = '%s/ISAPI/Streaming/channels/101/picture'
        try:
            response = self.hik_request.get(url % self.root_url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True)
            raw = io.BytesIO(response.content)
            with Image.open(raw) as img:
                _LOGGING.warning(f'---2  {box} {path}')
                if box:
                    img_crop = img.crop(box)
                    img_crop.save(path)
                    img.save(f'{path}.orig.jpg')
                else:
                    img.save(path)
        except Exception as e:
            _LOGGING.info(e)

    def update_attributes(self, event, channel, attr):
        """Update attribute list for current event/channel."""
        try:
            for i, sensor in enumerate(self.event_states[event]):
                if sensor[1] == int(channel):
                    self.event_states[event][i] = attr
        except KeyError:
            _LOGGING.debug('Error updating attributes for: (%s, %s)',
                           event, channel)

    def publish_changes(self, etype, echid, region=''):
        """Post updates for specified event type."""
        _LOGGING.debug('%s Update: %s, %s',
                       self.name, etype, self.fetch_attributes(etype, echid))
        signal = 'ValueChanged.{}'.format(self.cam_id)
        sender = '{}.{}'.format(etype, echid)
        if dispatcher:
            dispatcher.send(signal=signal, sender=sender)

        self._do_update_callback('{}.{}.{}{}'.format(self.cam_id, etype, echid,region))
