from decimal import Decimal
from rest_framework import serializers
from trade import consts

from trade.models import Instrument, FavoriteInstrument, Position, ClientTrade, Order
from trade.service import TradeService


class InstrumentSerializer(serializers.ModelSerializer):
    asset_class = serializers.Field(source='get_asset_class_display')
    favorite = serializers.SerializerMethodField('get_is_favorite')
    favorite_position = serializers.SerializerMethodField('get_favorite_position')

    def __init__(self, *args, **kwargs):
        super(InstrumentSerializer, self).__init__(*args, **kwargs)

        #Prefetch user favorites, for prevent db deluge
        favorites = FavoriteInstrument.objects.filter(
            user_id=self.context.get('request').user.id
        )
        self.favorite = {
            'instruments': favorites.values_list('instrument_id', flat=True),
            'positions': dict(favorites.values_list('instrument_id', 'position'))
        }

    def get_favorite_position(self, obj):
        return self.favorite['positions'].get(obj.id, None)

    def get_is_favorite(self, obj):
        return obj.id in self.favorite['instruments']

    class Meta:
        model = Instrument
        fields = ('name', 'symbol', 'asset_class', 'favorite', 'favorite_position')


class PositionCloseSerializer(serializers.Serializer):
    position = serializers.IntegerField()
    amount   = serializers.IntegerField()
    rate     = serializers.DecimalField(max_digits=25, decimal_places=6, required=False)

    def save(self):
        self.object['position'] = Position.objects.get(pk=self.object['position'])
        return self.object

class PositionCreateSerializer(serializers.Serializer):
    instrument  = serializers.ChoiceField()
    rate        = serializers.DecimalField(max_digits=25, decimal_places=6)
    amount      = serializers.IntegerField()
    side        = serializers.ChoiceField(choices=consts.SIDES)
    stop_loss_distance   = serializers.DecimalField()
    take_profit_distance = serializers.DecimalField(required=False)

    def __init__(self, *args, **kwargs):
        self.base_fields['instrument'].choices = [(i.url_slug, i.url_slug) for i in Instrument.objects.filter(active=True)]
        super(PositionCreateSerializer, self).__init__(*args, **kwargs)

    def save(self):
        self.object['instrument'] = Instrument.objects.get(url_slug=self.object['instrument'])
        self.object['side'] = int(self.object['side'])
        return self.object


class PositionSerializer(serializers.ModelSerializer):
    slug = serializers.Field(source='instrument.url_slug')
    get_upnl = serializers.SerializerMethodField('get_upnl')
    closed_amount = serializers.SerializerMethodField('get_closed_amount')
    last_client_trade = serializers.SerializerMethodField('get_last_client_trade')
    open_rate = serializers.SerializerMethodField('get_open_rate')
    stop_loss = serializers.SerializerMethodField('get_stop_loss')
    stop_loss_distance = serializers.SerializerMethodField('get_stop_loss_distance')
    take_profit_distance = serializers.SerializerMethodField('get_take_profit_distance')

    def get_open_rate(self, obj):
        return obj.instrument.quantize_price_down(Decimal(obj.open_rate))

    def get_stop_loss(self, obj):
        return obj.instrument.quantize_price_down(Decimal(obj.stop_loss))

    def get_stop_loss_distance(self, obj):
        # side, distance_rate, open_rate, instrument
        return TradeService._rate_to_distance_convert(
                    obj.side,
                    obj.stop_loss,
                    obj.open_rate,
                    obj.instrument
                )

    def get_take_profit_distance(self, obj):
        return obj.take_profit and TradeService._rate_to_distance_convert(
                    obj.side,
                    obj.take_profit,
                    obj.open_rate,
                    obj.instrument,
                    True
                )

    def get_last_client_trade(self, obj):
        client_trades = obj.clienttrade_set.order_by('-time')
        if client_trades.count() > 0:
            return client_trades[0].id
        return None

    def get_closed_amount(self, obj):
        return obj.opening_amount - obj.amount

    def get_upnl(self,obj):
        return obj.get_upnl()

    class Meta:
        model = Position
        fields = ('id', 'instrument', 'slug', 'open_rate', 'amount', 'side', 'stop_loss', 'take_profit', 'stop_loss_distance', 'take_profit_distance', 'open_date', 'last_modified', 'pnl', 'close_rate', 'closed_amount', 'state', 'last_client_trade')


class OrderSerializer(serializers.ModelSerializer):

    class Meta:
        model = Order
        fields = ('id', 'instrument', 'amount', 'side', 'expected_rate', )


class PlaceOrderSerializer(serializers.Serializer):
    instrument  = serializers.ChoiceField()
    amount      = serializers.IntegerField()
    side        = serializers.ChoiceField(choices=consts.SIDES)
    stop_loss_distance   = serializers.DecimalField()
    take_profit_distance = serializers.DecimalField(required=False)
    expected_rate        = serializers.DecimalField(max_digits=25, decimal_places=6)

    def __init__(self, *args, **kwargs):
        self.base_fields['instrument'].choices = [(i.url_slug, i.url_slug) for i in Instrument.objects.filter(active=True)]
        super(PlaceOrderSerializer, self).__init__(*args, **kwargs)

    def save(self):
        self.object['instrument'] = Instrument.objects.get(url_slug=self.object['instrument'])
        self.object['side'] = int(self.object['side'])
        return self.object


class CancelOrderSerializer(serializers.Serializer):
    order = serializers.IntegerField()

    def save(self):
        self.object['order'] = Order.objects.get(pk=self.object['order'])
        return self.object


class ClientTradeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientTrade
        fields = ('instrument', 'asked_rate', 'rate', 'amount', 'side', 'time')


class RequiredMarginSerializer(serializers.Serializer):
    side        = serializers.ChoiceField(choices=consts.SIDES)
    instrument  = serializers.ChoiceField()
    stop_loss_distance = serializers.DecimalField()
    amount             = serializers.IntegerField()
    rate               = serializers.DecimalField(max_digits=25, decimal_places=6)

    def __init__(self, *args, **kwargs):
        self.base_fields['instrument'].choices = [(i.url_slug, i.url_slug) for i in Instrument.objects.filter(active=True)]
        super(RequiredMarginSerializer, self).__init__(*args, **kwargs)

    def save(self):
        self.object['side'] = int(self.object['side'])
        self.object['instrument'] = Instrument.objects.get(url_slug=self.object['instrument'])
        self.object['stop_loss_distance'] = TradeService._distance_to_rate_convert(
            side=int(self.object['side']),
            distance=int(self.object['stop_loss_distance']),
            rate=Decimal(self.object['rate']),
            instrument=self.object['instrument']
        )
        return self.object
