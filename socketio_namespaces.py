from decimal import Decimal, ROUND_DOWN

from socketio.namespace import BaseNamespace
from gevent.greenlet import Greenlet
from gevent.event import AsyncResult

from django.conf import settings
from django.core.cache import cache

from .models import Instrument
from utils.pubsub import Connection, Consumer
from utils.pubsub_conf import PUBSUB_RATES_CONFIG


instruments = Instrument.objects.all()


class InstrumentsPriceNamespace(BaseNamespace):
    asyncres = AsyncResult()
    greenlet = None

    def recv_connect(self):
        self.greenlet = Greenlet.spawn(self.listener)

    def recv_disconnect(self):
        if self.greenlet is not None:
            self.greenlet.kill()

    def listener(self):
        while True:
            msg = InstrumentsPriceNamespace.asyncres.get()
            self.send({msg['asset']: msg}, json=True)

    @staticmethod
    def start_pubsub():
        Greenlet.spawn(InstrumentsPriceNamespace.pubsub_consumer)

    @staticmethod
    def pubsub_consumer():
        with Connection(settings.PUBSUB_URL) as conn:
            Consumer(conn, PUBSUB_RATES_CONFIG, callback=InstrumentsPriceNamespace.broadcast_message).run()

    @staticmethod
    def broadcast_message(msg):
        for instrument in instruments:
            if instrument.url_slug == msg['asset']:
                # print 'sending', instrument.symbol
                rates = {
                    'sell': instrument.quantize_price_down(Decimal(msg['sell'])),
                    'buy': instrument.quantize_price_down(Decimal(msg['buy'])),
                    'low': instrument.quantize_price_down(Decimal(msg['low'])),
                    'high': instrument.quantize_price_down(Decimal(msg['high'])),
                }
                cache.set('rates_%s' % instrument.url_slug, rates)
                msg['buy'] = str(rates['buy'])
                msg['sell'] = str(rates['sell'])

                InstrumentsPriceNamespace.asyncres.set(msg)
                InstrumentsPriceNamespace.asyncres = AsyncResult()
