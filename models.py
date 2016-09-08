from datetime import timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from django.db import models
from django.contrib.auth.models import User
from django.db.utils import IntegrityError
from django.utils import timezone
from django.conf import settings

from timezone_field import TimeZoneField
from easy_thumbnails.fields import ThumbnailerImageField

from tc_instruments import models as instruments_models

from currency.models import Currency
import consts


class OpenTimeGroup(models.Model):
    name = models.CharField(max_length=128, unique=True)
    timezone = TimeZoneField(default="UTC")

    def __unicode__(self):
        return "%s (%s)" % (self.name, self.timezone)


class OpenTimeRange(models.Model):
    weekday     = models.SmallIntegerField(choices=consts.DAYS_OF_WEEK)
    time_from   = models.TimeField()
    time_to     = models.TimeField()
    open_time_group = models.ForeignKey(OpenTimeGroup)


class Marketplace(models.Model):
    name = models.CharField(max_length=128, unique=True)


class Instrument(instruments_models.BaseInstrument):
    url_slug                = models.CharField(max_length=128, unique=True, verbose_name='Slug')
    description             = models.CharField(max_length=255, unique=True)
    markup_mapping          = models.CharField(max_length=255, unique=True)
    chart_code              = models.CharField(max_length=128, null=True, blank=True)
    news_code               = models.CharField(max_length=128, null=True, blank=True)
    base_asset              = models.CharField(max_length=30)
    quote_asset             = models.ForeignKey(Currency)
    #todo Phase2: liquidity_provider = models.Field() we should decide what type it should be and how do we use it
    #todo Phase2: exchange = models.SmallIntegerField()
    #todo: minimum stop distance - offset for minimum stop loss setting it replaces the stoploss delta
    stop_distance_absolute  = models.BooleanField()
    minimum_stop_distance   = models.DecimalField(max_digits=25, decimal_places=2)
    slippage_absolute       = models.BooleanField()
    slippage                = models.DecimalField(max_digits=25, decimal_places=2)
    #todo: minimum margin absolute/percent + add logic of the margin calculations on this
    minimum_margin_absolute = models.BooleanField()
    minimum_margin          = models.DecimalField(max_digits=25, decimal_places=2)
    min_trade_size          = models.SmallIntegerField(default=1, verbose_name='Min size', help_text='Min trade size')
    trade_size_increment    = models.SmallIntegerField(default=1, verbose_name='Size incr', help_text='Trade size increment')
    tradable                = models.BooleanField(default=True)
    new_positions_allowed   = models.BooleanField(default=True, verbose_name='New pos', help_text='New positions allowed')
    shortable               = models.BooleanField(default=True)
    tick_size               = models.DecimalField(max_digits=25, decimal_places=6, default=1)
    display_tick_size       = models.DecimalField(max_digits=25, decimal_places=6, default=1, verbose_name='Tick size UI', help_text='Display tick size')
    open_time_group         = models.ForeignKey(OpenTimeGroup)
    interest                = models.DecimalField(max_digits=25, decimal_places=6, default=0.00001)
    logo = ThumbnailerImageField(upload_to='uploads/logos', blank=True, null=True)
    contract_size = models.IntegerField(default=1, verbose_name='Contract size', help_text='Size of contract')

    def is_allowed_by_time(self):
        """
        Defines if the instrument related exchange is operational
        """
        if getattr(settings, 'DEBUG_SKIP_TIME_CHECK', False):
            return True

        current_time = timezone.now()
        date_time = current_time - timedelta(microseconds=current_time.microsecond)
        day_of_week = date_time.weekday()
        current_time = date_time.timetz()

        time_ranges = self.open_time_group.opentimerange_set.filter(weekday=day_of_week)
        for time_range in time_ranges:
            time_from = timezone.make_aware(time_range.time_from, self.open_time_group.timezone)
            time_to = timezone.make_aware(time_range.time_to, self.open_time_group.timezone)
            if time_from <= current_time <= time_to:
                return True
        return False

    def is_accessible_for_action(self):
        """
        Defines if the requested operation on the instrument is allowed
        """
        return self.active and self.tradable and self.is_allowed_by_time()

    def is_position_openable(self, side):
        shortable = True
        if not self.shortable and side == consts.TYPE_SELL:
            shortable = False
        return self.is_accessible_for_action() and self.new_positions_allowed and shortable

    def is_amount_tradable(self, amount):
        properly_incremented = amount % self.trade_size_increment == 0
        proper_amount = amount >= self.min_trade_size
        return properly_incremented and proper_amount

    def readable_stop_distance(self):
        if self.stop_distance_absolute:
            return self.minimum_stop_distance
        return str(self.minimum_stop_distance) + '%'

    def readable_slippage(self):
        if self.slippage_absolute:
            return self.slippage
        return str(self.slippage) + '%'

    def readable_min_margin(self):
        if self.minimum_margin_absolute:
            return self.minimum_margin
        return str(self.minimum_margin) + '%'

    def quantize_price_down(self, value):
        return Decimal(value).quantize(self.display_tick_size.normalize(), rounding=ROUND_DOWN)

    def quantize_price_up(self, value):
        return Decimal(value).quantize(self.display_tick_size.normalize(), rounding=ROUND_UP)


class Position(models.Model):

    user           = models.ForeignKey(User)
    instrument     = models.ForeignKey(Instrument)
    marketplace    = models.ForeignKey(Marketplace)
    opening_amount = models.IntegerField(default=0)
    amount         = models.IntegerField(default=0)
    asked_rate     = models.DecimalField(max_digits=25, decimal_places=6)  # rate that user requested
    open_rate      = models.DecimalField(max_digits=25, decimal_places=6, null=True, blank=True)  # position opened at
    close_rate     = models.DecimalField(max_digits=25, decimal_places=6, null=True, blank=True)  # position closed at
    side           = models.SmallIntegerField(choices=consts.SIDES)
    stop_loss      = models.DecimalField(max_digits=25, decimal_places=6)
    asked_stop_distance = models.DecimalField(max_digits=25, decimal_places=2, default=0)
    take_profit    = models.DecimalField(max_digits=25, decimal_places=6, null=True, blank=True)
    state          = models.SmallIntegerField(choices=consts.POSITION_STATES, default=consts.STATE_PENDING)
    current_margin = models.DecimalField(max_digits=25, decimal_places=2)
    pnl            = models.DecimalField(max_digits=25, decimal_places=2, default=0)
    open_date      = models.DateTimeField(auto_now_add=True)
    close_date      = models.DateTimeField(null=True, blank=True)
    last_modified  = models.DateTimeField(auto_now=True)

    def __unicode__(self):
        return "position %d" % self.pk

    def is_open(self):
        return self.state < consts.STATE_CLOSED

    def is_pending(self):
        return self.state == consts.STATE_PENDING

    def get_side(self):
        return dict(consts.SIDES)[self.side]

    def get_state(self):
        return dict(consts.POSITION_STATES)[self.state]

    def get_upnl(self):
        try:
            if self.state in (consts.STATE_OPENED, consts.STATE_PARTIALLY_CLOSED):
                from trade.service import TradeService
                rate = TradeService.get_rates(self.instrument, self.user)
                if self.side == consts.TYPE_SELL:
                    current_rate = rate['buy']
                    multiplier = - 1
                else:
                    current_rate = rate['sell']
                    multiplier = 1
                return multiplier * (current_rate - self.open_rate) * self.amount
            else:
                return 0
        except:
            return 0


class Order(models.Model):
    user                 = models.ForeignKey(User, related_name='orders')
    instrument           = models.ForeignKey(Instrument, related_name='orders')
    amount               = models.IntegerField(default=0)
    side                 = models.SmallIntegerField(choices=consts.SIDES)
    asked_stop_distance  = models.DecimalField(max_digits=25, decimal_places=2, default=0)
    take_profit_distance = models.DecimalField(max_digits=25, decimal_places=2, null=True, blank=True)
    expected_rate        = models.DecimalField(max_digits=25, decimal_places=6)
    position             = models.OneToOneField(Position, related_name='order', null=True, blank=True)
    state                = models.SmallIntegerField(choices=consts.ORDER_STATES)
    open_date            = models.DateTimeField(auto_now_add=True)
    last_modified        = models.DateTimeField(auto_now=True)


class HouseTrade(models.Model):
    instrument  = models.ForeignKey(Instrument)
    marketplace = models.ForeignKey(Marketplace)
    rate        = models.DecimalField(max_digits=25, decimal_places=5)
    amount      = models.IntegerField(default=0)
    success     = models.BooleanField()
    side        = models.SmallIntegerField(choices=consts.SIDES)
    time        = models.DateTimeField(auto_now_add=True)

    def __unicode__(self):
        return "HouseTrade %d" % self.pk

    def get_side(self):
        return dict(consts.SIDES)[self.side]


class ClientTrade(models.Model):

    user           = models.ForeignKey(User)
    instrument     = models.ForeignKey(Instrument)
    position       = models.ForeignKey(Position)
    asked_rate     = models.DecimalField(max_digits=25, decimal_places=5)
    rate           = models.DecimalField(max_digits=25, decimal_places=5)
    amount         = models.IntegerField(default=0)
    position_state = models.SmallIntegerField(choices=consts.POSITION_STATES)
    success        = models.BooleanField()
    side           = models.SmallIntegerField(choices=consts.SIDES)
    time           = models.DateTimeField(auto_now_add=True)
    channel        = models.SmallIntegerField(choices=consts.CHANNELS, default=consts.CHANNEL_WEB)
    house_trade    = models.ForeignKey(HouseTrade, null=True, blank=True)

    def get_position_state(self):
        return dict(consts.POSITION_STATES)[self.position_state]

    def get_side(self):
        return dict(consts.SIDES)[self.side]


class FavoriteInstrument(models.Model):
    user = models.ForeignKey(User)
    instrument = models.ForeignKey(Instrument)

    position = models.PositiveIntegerField('Position in favorites', null=True)

    def save(self, *args, **kwargs):
        #We can't add inactive instrument to favorites
        if not self.instrument.active:
            return None

        #Get instruments count for current user
        try:
            max_position = FavoriteInstrument.objects.filter(
                user_id=self.user.id
            ).order_by('-position')[0].position
        except IndexError:
            max_position = 0

        #For new record or if position out of range will compute it
        if not self.pk or self.position > max_position:
            self.position = max_position + 1

        #It is needed because this instrument might be already in favorites
        #than exception will be raised
        try:
            return super(FavoriteInstrument, self).save(*args, **kwargs)
        except IntegrityError:
            return None

    def delete(self, *args, **kwargs):
        super(FavoriteInstrument, self).delete(*args, **kwargs)
        #Normalize positions
        FavoriteInstrument.objects.filter(
            user_id=self.user_id,
            position__gt=self.position
        ).update(position=models.F('position')-1)

    def __unicode__(self):
        return '%s in %s favorites' % (self.instrument.name, self.user.username)

    class Meta:
        unique_together = ('instrument', 'user')
        index_together = [
            ['instrument', 'user'],
        ]


class EndOfDayRate(models.Model):
    instrument = models.ForeignKey(Instrument)
    date = models.DateField()
    rate = models.DecimalField(max_digits=25, decimal_places=6)

    class Meta:
        unique_together = ('instrument', 'date')


from .signals import *