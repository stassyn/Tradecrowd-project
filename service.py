from decimal import Decimal, ROUND_DOWN, ROUND_UP
from activity.models import Post
from trade import consts
from wallet.service import WalletService, Overdraft
from currency.service import CurrencyService

from django.utils import timezone
import celery

from apps.utils.mixpanel_tasks import track_user
from .models import Position, ClientTrade, HouseTrade, EndOfDayRate, Marketplace, Order
from accounts.models import Profitability


class WrongAmount(Exception):
    pass


class InstrumentNotTradeable(Exception):
    pass


class ZeroRate(Exception):
    pass


class TradeService(object):
    client = None

    @staticmethod
    def open_position(user, instrument, rate, amount, side, stop_loss_distance, take_profit_distance=None, order=None):
        if instrument.is_position_openable(side=side):
            if instrument.is_amount_tradable(amount):
                #calculate and check the stop loss rate
                stop_loss_rate = TradeService._get_stoploss_rate(
                    instrument=instrument,
                    stop_loss_distance=stop_loss_distance,
                    rate=rate,
                    side=side
                )

                #calculate and check the take profit
                if not take_profit_distance is None:
                    take_profit_rate = TradeService._distance_to_rate_convert(
                        side=side,
                        distance=take_profit_distance,
                        rate=rate,
                        instrument=instrument,
                        is_take_profit=True)
                else:
                    take_profit_rate = None

                #calculate cash required for margin - pretrade validation
                cash_to_margin = TradeService._calculate_margin(
                    side=side,
                    instrument=instrument,
                    stop_loss_rate=stop_loss_rate,
                    amount=amount,
                    rate=rate,
                )

                print '%d -- %d '%(cash_to_margin, WalletService(user).get_useful_balance())
                #check wallet for cash required for margin
                if cash_to_margin < WalletService(user).get_useful_balance():
                    marketplace = Marketplace.objects.get(pk=settings.DEFAULT_MARKETPLACE)
                    #create position with status pending
                    position = Position.objects.create(user=user,
                                                       instrument=instrument,
                                                       marketplace=marketplace,
                                                       opening_amount=amount,
                                                       amount=amount,
                                                       asked_rate=rate,
                                                       open_rate=rate,
                                                       side=side,
                                                       stop_loss=stop_loss_rate,
                                                       asked_stop_distance=stop_loss_distance,
                                                       take_profit=take_profit_rate,
                                                       current_margin=cash_to_margin)

                    #issue trade
                    position.save()
                    TradeService.issue_trade(position, rate, amount, side)
                    if order is not None:
                        order.position = position
                        order.save()
                    return position.id
                else:
                    raise Overdraft
            else:
                raise WrongAmount
        else:
            raise InstrumentNotTradeable

    #region Orders
    @staticmethod
    def place_conditional_order(user, instrument, expected_rate, amount, side, stop_loss_distance, take_profit_distance=None):
        order = Order.objects.create(
            user=user,
            instrument=instrument,
            amount=amount,
            side=side,
            asked_stop_distance=stop_loss_distance,
            take_profit_distance=take_profit_distance,
            expected_rate=expected_rate,
            state=consts.STATE_PENDING
        )

        #set the execution worker on condition reach
        TradeService.client.place_order(order)
        return order.id

    @staticmethod
    def cancel_order(order):
        if isinstance(order, int):
            order = Order.objects.get(id=order)
        order.state = consts.STATE_CANCELED

        #find the execution worker and remove task
        TradeService.client.cancel_order(order.id)
        order.save()
    #endregion Orders

    #region Close Position
    @staticmethod
    def close_position(position, amount, rate=None, close_reason=None):
        if amount > position.amount:
            raise WrongAmount
        #todo: rate should be defined in case of limit trade in the rest of the cases "current" should be taken (Market)
        if rate is None:
            rates = TradeService.get_rates(position.instrument, position.user)
            if position.side == consts.TYPE_BUY:
                rate = rates['sell']
            else:
                rate = rates['buy']

        if position.side == consts.TYPE_BUY:
            TradeService.issue_trade(position, rate, amount, consts.TYPE_SELL, close_reason)
        else:
            TradeService.issue_trade(position, rate, amount, consts.TYPE_BUY, close_reason)

    @staticmethod
    def close_all_positions(user):
        positions = Position.objects.filter(user=user, state__lt=consts.STATE_CLOSED)
        for position in positions:
            TradeService.close_position(position, position.amount)
    #endregion Close Position

    @staticmethod
    def issue_trade(position, rate, amount, side, close_reason=None):
        TradeService._trade_request(position, rate, amount, side, close_reason)

    @staticmethod
    def stop_loss_take_profit(position, is_take_profit=False):
        if is_take_profit:
            TradeService.close_position(position, position.amount, close_reason=consts.STATE_CLOSED_TAKE_PROFIT)
        else:
            TradeService.close_position(position, position.amount, close_reason=consts.STATE_CLOSED_STOPLOSS)

    @staticmethod
    def change_stop_loss(position, stop_loss_rate):
        #todo: ask Stefan about case of margin nullification
        new_margin = TradeService._calculate_margin(
            side=position.side,
            instrument=position.instrument,
            stop_loss_rate=stop_loss_rate,
            amount=position.amount,
            rate=position.open_rate
        )
        if position.current_margin > new_margin:
            WalletService(position.user).release_margin(
                amount=position.current_margin - new_margin,
                currency=position.instrument.quote_asset,
                position=position
            )
        elif position.current_margin < new_margin:
            WalletService(position.user).reserve_margin(
                amount=new_margin - position.current_margin,
                currency=position.instrument.quote_asset,
                position=position
            )
        else:
            pass
        position.current_margin = new_margin
        position.save()

    @staticmethod
    def _get_stoploss_rate(instrument, stop_loss_distance, rate, side):
        #calculate and check the stop loss rate
        instrument_distance = TradeService._get_instrument_min_distance(instrument, rate)
        if stop_loss_distance < instrument_distance:
            stop_loss_distance = instrument_distance
        stop_loss_rate = TradeService._distance_to_rate_convert(
            side=side,
            distance=stop_loss_distance,
            rate=rate,
            instrument=instrument)
        return stop_loss_rate

    @staticmethod
    def _get_instrument_min_distance(instrument, rate):
        """
            Returns the instrument min distance in absolute values
        """
        if instrument.stop_distance_absolute:
            return instrument.minimum_stop_distance
        else:
            #todo: ask Stefan if we should round it
            return Decimal(instrument.minimum_stop_distance) / 100 * rate / instrument.tick_size

    @staticmethod
    def _distance_to_rate_convert(side, distance, rate, instrument, is_take_profit=False):
        if side == consts.TYPE_BUY:
            multiplier = 1
        else:
            multiplier = -1
        if is_take_profit:
            multiplier *= -1
        return Decimal(rate) - Decimal(distance) * instrument.tick_size * multiplier


    @staticmethod
    def _rate_to_distance_convert(side, distance_rate, open_rate, instrument, is_take_profit=False):
        if side == consts.TYPE_BUY:
            multiplier = 1
        else:
            multiplier = -1
        if is_take_profit:
            multiplier *= -1
        return (Decimal(open_rate) - Decimal(distance_rate)) / instrument.tick_size / multiplier

    @staticmethod
    def _calculate_margin(side, instrument, stop_loss_rate, amount, rate):
        if side == consts.TYPE_BUY:
            multiplier = 1
        else:
            multiplier = -1

        #first calculate the minimum margin in cash (todo:how?)
        if instrument.minimum_margin_absolute:
            minimum_margin = instrument.minimum_margin * instrument.tick_size
            # test_trade_formulas._absolute_minimum_margin
        else:
            minimum_margin = Decimal(instrument.minimum_margin) / 100 * Decimal(rate)
            # test_trade_formulas._relative_minimum_margin

        stop_distance = abs(rate - stop_loss_rate)

        #calculate slippage for a margin purpose aligned with rate
        if instrument.slippage_absolute:
            slippage_rate = Decimal(instrument.slippage) * instrument.tick_size
            # test_trade_formulas._absolute_slippage
        else:
            slippage_rate = Decimal(instrument.slippage) / 100 * Decimal(minimum_margin)
            # test_trade_formulas._relative_slippage

        cash = (Decimal(max(stop_distance, minimum_margin)) + slippage_rate) * Decimal(amount)
        #todo: calculations of slippage/minimum_margin/etc should be placed in separate function to improve maintainability

        if cash > 0:
            return instrument.quote_asset.quantize_value_up(cash)
        else:
            return 0

    @staticmethod
    def _trade_request(position, rate, amount, side, close_reason=None):
        #todo: here we should decide if we are creating the hedge trade
        TradeService.client.trade_request(
            position.pk,
            position.instrument.symbol,
            rate,
            amount,
            side,
            close_reason
        )

    @staticmethod
    def _trade_callback(position_pk, success, symbol, amount, side, rate, hedged=False, close_reason=None):
        # print "------------"
        # print position_pk, success, symbol, amount, side, rate, hedged
        position = Position.objects.get(pk=position_pk)
        #todo: should check if the trade is done according to position amount, instrument etc and if no - undo
        if hedged:
            house_trade = HouseTrade.objects.create(
                instrument=position.instrument,
                marketplace=position.marketplace,
                rate=rate,
                amount=amount,
                success=success,
                side=side
            )
            trade = ClientTrade.objects.create(
                user=position.user,
                instrument=position.instrument,
                position=position,
                asked_rate=position.asked_rate,
                rate=rate,
                amount=amount,
                position_state=position.state,
                success=success,
                side=side,
                house_trade=house_trade
            )
        else:
            trade = ClientTrade.objects.create(
                user=position.user,
                instrument=position.instrument,
                position=position,
                asked_rate=position.asked_rate,
                rate=rate,
                amount=amount,
                position_state=position.state,
                success=success,
                side=side
            )
        #if successful continue else create failed position
        if position.state == consts.STATE_PENDING:
            if success:
                position.open_rate = trade.rate
                #reserve cash
                try:
                    stop_loss_rate = TradeService._get_stoploss_rate(
                        instrument=position.instrument,
                        stop_loss_distance=position.asked_stop_distance,
                        rate=trade.rate,
                        side=position.side
                    )
                    cash_to_margin = TradeService._calculate_margin(
                        side=position.side,
                        instrument=position.instrument,
                        stop_loss_rate=stop_loss_rate,
                        amount=position.amount,
                        rate=position.open_rate,
                    )
                    WalletService(position.user).reserve_margin(
                        amount=cash_to_margin,
                        currency=position.instrument.quote_asset,
                        trade=trade,
                        position=position
                    )
                    position.current_margin = cash_to_margin
                    #if reserve successful open position, else(overdraft) - fail with status "Not enough cash to open"
                    position.state = consts.STATE_OPENED
                    position.stop_loss = stop_loss_rate
                except Overdraft:
                    #todo: add logic to reverse trade
                    position.state = consts.STATE_MARGIN_FAILED
            else:
                position.state = consts.STATE_OPEN_FAILED

            position.save()
            track_user.delay(
                position.user,
                'Position Opened',
                {
                    'instrument': position.instrument.base_asset,
                    'side': position.side
                }
            )
            TradeService._post_position_update(position, trade)
        elif position.state in (consts.STATE_OPENED, consts.STATE_PARTIALLY_CLOSED):
            #count PnL multiplier
            if position.side == consts.TYPE_BUY:
                pnl_multiplier = 1
            else:
                pnl_multiplier = -1
            if success:
                if position.amount > trade.amount:
                    position.state = consts.STATE_PARTIALLY_CLOSED
                    position.amount -= trade.amount
                    #free part of margin
                    new_margin = TradeService._calculate_margin(
                        side=position.side,
                        instrument=position.instrument,
                        stop_loss_rate=position.stop_loss,
                        amount=position.amount,
                        rate=position.open_rate,
                    )
                    cash_to_release = position.current_margin - new_margin
                    WalletService(position.user).release_margin(
                        amount=cash_to_release,
                        currency=position.instrument.quote_asset,
                        trade=trade,
                        position=position
                    )
                    position.current_margin = new_margin
                    position.close_rate = trade.rate

                    position.save()
                    #apply PnL on traded account
                    pnl_value = Decimal(trade.amount) * (Decimal(trade.rate) - position.open_rate) * pnl_multiplier
                    TradeService._process_pnl(pnl_value, position, trade)

                    #todo: post a post
                    TradeService._post_position_update(position, trade)
                    track_user.delay(
                        position.user,
                        'Position partially closed',
                        {
                            'instrument': position.instrument.base_asset,
                            'side': position.side
                        }
                    )
                elif position.amount == trade.amount:
                    if close_reason:
                        position.state = close_reason
                    else:
                        position.state = consts.STATE_CLOSED
                    position.amount = 0
                    position.close_rate = trade.rate
                    position.close_date = timezone.now()
                    #free margin
                    WalletService(position.user).release_margin(
                        amount=position.current_margin,
                        currency=position.instrument.quote_asset,
                        trade=trade,
                        position=position
                    )
                    position.current_margin = 0
                    position.save()
                    #apply PnL on traded account
                    pnl_value = Decimal(trade.amount) * (Decimal(trade.rate) - position.open_rate) * pnl_multiplier
                    TradeService._process_pnl(pnl_value, position, trade)
                    #todo: post a post
                    TradeService._post_position_update(position, trade)
                    track_user.delay(
                        position.user,
                        'Position closed',
                        {
                            'instrument': position.instrument.base_asset,
                            'side': position.side
                        }
                    )
        trade.position_state = position.state
        trade.save()

    @staticmethod
    def _process_pnl(pnl_value, position, trade):
        #apply pnl on the wallet
        pnl_value = position.instrument.quote_asset.quantize_value_down(pnl_value)
        WalletService(position.user).apply_pnl(
            pnl_value,
            position.instrument.quote_asset,
            trade
        )
        position.pnl += pnl_value
        position.save()
        if pnl_value >= 0:
            track_user.delay(
                position.user,
                'Positive PnL',
                {
                    'instrument': position.instrument.base_asset,
                    'side': position.side
                }
            )
        else:
            track_user.delay(
                position.user,
                'Negative PnL',
                {
                    'instrument': position.instrument.base_asset,
                    'side': position.side
                }
            )
        #update profitability table
        #todo: should decide if all profitability update should occur on position close or on apply pnl
        #todo: recount the profitability in single currency
        if position.state == consts.STATE_CLOSED:
            user_base_currency_value = CurrencyService.convert_value(
                                       position.instrument.quote_asset,
                                       position.user.profile.currency,
                                       position.pnl)
            profit_by_asset = Profitability.objects.get_or_create(
                user=position.user,
                asset_class=position.instrument.asset_class
            )[0]
            profit_by_asset.pnl += user_base_currency_value

            profit_by_instrument = Profitability.objects.get_or_create(
                user=position.user,
                instrument=position.instrument
            )[0]
            profit_by_instrument.pnl += user_base_currency_value

            profit_by_positions = Profitability.objects.get_or_create(
                user=position.user,
                asset_class__isnull=True,
                instrument__isnull=True
            )[0]
            profit_by_positions.pnl += user_base_currency_value

            profit_by_asset.positions += 1
            profit_by_instrument.positions += 1
            profit_by_positions.positions += 1

            profit_by_asset.save()
            profit_by_instrument.save()
            profit_by_positions.save()

    @staticmethod
    def _post_position_update(position, trade):
        Post.objects.create_trade_post(
            state=position.state,
            side=position.side,
            user=position.user,
            position=position,
            price=trade.rate)

    @staticmethod
    def get_rates(instrument, user):
        client_rates = TradeService.client.get_rates(instrument, user)
        # for type, rate in client_rates.items():
        #     if rate == 0:
        #         raise ZeroRate
        return {
            'sell': instrument.quantize_price_down(Decimal(client_rates['sell'])),
            'buy': instrument.quantize_price_down(Decimal(client_rates['buy'])),
            'low': instrument.quantize_price_down(Decimal(client_rates['low'])),
            'high': instrument.quantize_price_down(Decimal(client_rates['high'])),
        }

    @staticmethod
    def get_eod_rate(instrument):
        try:
            return EndOfDayRate.objects.filter(instrument=instrument).order_by('-date')[0].rate
        except:
            return Decimal('0.0')


class DummyClient():
    """
    Dummy client that pretend to be an work with API of liquidity provider
    """

    def trade_request(self,
                      position_pk,
                      instrument_symbol,
                      requested_rate,
                      amount,
                      side,
                      close_reason=None,
                      market_or_limit_type="Market"):
        # fake call of supposedly-asynchronous function
        self.on_trade_result(position_pk=position_pk,
                             success=True,
                             symbol=instrument_symbol,
                             amount=amount,
                             side=side,
                             rate=requested_rate,
                             close_reason=close_reason
        )

    def on_trade_result(self, position_pk, success, symbol, amount, side, rate, close_reason=None):
        position = Position.objects.get(pk=position_pk)
        if position.side != side and position.amount == amount:
            TradeService._trade_callback(position_pk, success, symbol, amount, side, rate, True, close_reason)
        else:
            TradeService._trade_callback(position_pk, success, symbol, amount, side, rate, True)

    def place_order(self, order):
        self.on_order_condition_match(order.id)

    def cancel_order(self, order_id):
        #remove order from queue
        pass

    def on_order_condition_match(self, order_id):
        from .tasks import execute_order
        execute_order(order_id)

    def get_rates(self, instrument, user):
        return {
            'sell': 1400.00,
            'buy': 1600.00,
            'high': 1601.00,
            'low': 1399.00
        }




class Dummy2Client():
    """
    Dummy client that pretend to be an work with API of liquidity provider
    """

    def trade_request(self,
                      position_pk,
                      instrument_symbol,
                      requested_rate,
                      amount,
                      side,
                      close_reason=None,
                      market_or_limit_type="Market"):
        # fake call of supposedly-asynchronous function
        self.on_trade_result(position_pk=position_pk,
                             success=True,
                             symbol=instrument_symbol,
                             amount=amount,
                             side=side,
                             rate=requested_rate,
                             close_reason=close_reason
        )

    def place_order(self, order):
        self.on_order_condition_match(order.id)

    def cancel_order(self, order_id):
        #remove order from queue
        pass

    def on_order_condition_match(self, order_id):
        from .tasks import execute_order
        execute_order.delay(order_id)

    def on_trade_result(self, position_pk, success, symbol, amount, side, rate, close_reason=None):
        position = Position.objects.get(pk=position_pk)
        from .tasks import trade_result_from_client

        # add some random unsecsessfull transaction
        from random import randint
        success = randint(0,4) % 3 != 0

        if position.side != side and position.amount == amount:
            trade_result_from_client.apply_async(
                (position_pk, success, symbol, amount, side, rate, True, close_reason),
                countdown=5)
            # TradeService._trade_callback(position_pk, success, symbol, amount, side, rate, True, close_reason)
        else:
            trade_result_from_client.apply_async(
                (position_pk, success, symbol, amount, side, rate, True, None),
                countdown=5)
            # TradeService._trade_callback(position_pk, success, symbol, amount, side, rate, True)

    def get_rates(self, instrument, user):
        return {
            'sell': 1300.00,
            'buy': 1700.00,
            'high': 1701.00,
            'low': 1299.00
        }

from django.conf import settings
from django.core.cache import cache

from utils.pubsub import Connection, Publisher
from utils.pubsub_conf import PUBSUB_SEND_TRADES_CONFIG


class TradeClient(object):
    publisher = None

    def __init__(self):
        conn = Connection(settings.PUBSUB_URL)
        self.publisher = Publisher(conn, PUBSUB_SEND_TRADES_CONFIG)

    def get_rates(self, instrument, user):
        return cache.get('rates_%s' % instrument.url_slug, default={
            'sell': 0,
            'buy': 0,
            'high': 0,
            'low': 0
        })

    def trade_request(self, position_pk, instrument_symbol, requested_rate, amount, side,
                      market_or_limit_type="Market"):
        self.publisher.publish({
            'event': 'trade',
            'position_id': position_pk,
            'symbol': instrument_symbol,
            'rate': str(requested_rate),
            'amount': amount,
            'side': side,
            'type': market_or_limit_type
        })


    def place_order(self, order):
        #following is trigger logic
        if order.side == 0 and self.get_rates(order.instrument, order.user)['buy'] > order.expected_rate:
            self.on_order_condition_match(order.id)
        if order.side == 1 and self.get_rates(order.instrument, order.user)['sell'] < order.expected_rate:
            self.on_order_condition_match(order.id)

    def cancel_order(self, order_id):
        #remove order from queue
        pass

    def on_order_condition_match(self, order_id):
        from .tasks import execute_order
        execute_order.delay(order_id)

if getattr(settings, 'USE_DUMMY_TRADE_CLIENT', False):
    DUMMY_CLIENTS=[DummyClient, Dummy2Client]
    TradeService.client = DUMMY_CLIENTS[getattr(settings, 'DUMMY_CLIENT_CLASS', 0)]()
else:
    TradeService.client = TradeClient()
