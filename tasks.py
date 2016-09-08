from datetime import datetime, date as datetime_date
import celery

from django.conf import settings

import mongoengine

from .models import Order
from .mongo_models import FixTradeMsg
from .service import TradeService
from trade import consts


@celery.task
def trade_result_from_client(position_id, success, symbol, amount, side, rate, hedged, close_reason):
    TradeService._trade_callback(position_id, success, symbol, amount, side, rate, hedged, close_reason)


@celery.task
def save_fix_trade_msg(way, name, body, message, date):
    if name == 'HeartBeat':
        return
    if settings.MONGO_DATABASES['fix_trades']:
        mongoengine.connect(**settings.MONGO_DATABASES['fix_trades'])

        # convert datetime to string
        for k in message.keys():
            if isinstance(message[k], datetime) or isinstance(message[k], datetime_date):
                message[k] = str(message[k])
        # save the message to MongoDB
        msg_doc = FixTradeMsg(way=way, name=name, body=body, message=message, date=date)
        msg_doc.save(write_concern={'fsync': True})

@celery.task
def execute_order(order):
    if isinstance(order, int):
        order = Order.objects.get(id=order)
    TradeService.open_position(
        user=order.user,
        instrument=order.instrument,
        rate=order.expected_rate,
        amount=order.amount,
        side=order.side,
        stop_loss_distance=order.asked_stop_distance,
        take_profit_distance=order.take_profit_distance,
        order=order
    )
    order.state = consts.STATE_EXECUTED
    order.save()