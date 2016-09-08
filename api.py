from django.db.models import F
from rest_framework.status import HTTP_403_FORBIDDEN

from tc_instruments.models import BaseInstrument

from accounts.service import AccountService
from trade.models import Instrument, FavoriteInstrument, ClientTrade
from trade.serializers import InstrumentSerializer, PositionSerializer, PositionCreateSerializer, PositionCloseSerializer, ClientTradeSerializer, RequiredMarginSerializer, PlaceOrderSerializer, CancelOrderSerializer, OrderSerializer
from trade.service import TradeService, InstrumentNotTradeable, Overdraft, WrongAmount

from rest_framework import status, permissions, viewsets, mixins, generics
from rest_framework.response import Response
from rest_framework.exceptions import APIException


class InstrumentNotTradeableApi(APIException):
    detail = "Instrument is inaccessible for trading"
    status_code = HTTP_403_FORBIDDEN


class WrongAmountApi(APIException):
    detail = "Wrong amount provided for operation"
    status_code = HTTP_403_FORBIDDEN


class WrongOpeningAmountApi(APIException):
    detail = None
    status_code = HTTP_403_FORBIDDEN

    def __init__(self, increment, amount):
        self.detail = "The amount should be multiples of %s and more or equal to %s" % (increment, amount)


class OverdraftApi(APIException):
    detail = "There is not enough cash in your wallets"
    status_code = HTTP_403_FORBIDDEN


class InstrumentViewSet(viewsets.ModelViewSet):
    lookup_field = 'symbol'
    model = Instrument
    serializer_class = InstrumentSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    def get_bool_from_str(self, value):
        return {
            'true': True,
            'false': False
        }.get(value, None)

    def get_queryset(self):
        qs = Instrument.objects.filter(active=True)
        favorite = self.get_bool_from_str(
            self.request.QUERY_PARAMS.get('favorite')
        )

        if favorite is True:
            qs = qs.filter(
                favoriteinstrument__user_id=self.request.user.id
            ).order_by('favoriteinstrument__position')
        elif favorite is False:
            qs = qs.exclude(
                favoriteinstrument__user_id=self.request.user.id
            )

        return qs

    def filter_queryset(self, queryset):
        asset_class = self.request.QUERY_PARAMS.get('asset_class', None)
        if asset_class:
            try:
                asset_classes = dict(BaseInstrument.ASSET_CLASSES)
                asset_classes = dict(zip(asset_classes.values(), asset_classes.keys()))

                return queryset.filter(asset_class=asset_classes[asset_class])
            except KeyError:
                return []

        return queryset

    def update(self, request, *args, **kwargs):
        action = self.get_bool_from_str(
            self.request.DATA.get('favorite')
        )

        instrument = self.get_object()

        if action is True:
            FavoriteInstrument.objects.get_or_create(
                user_id=request.user.id,
                instrument=instrument
            )
        elif action is False:
            try:
                FavoriteInstrument.objects.get(
                    user_id=request.user.id,
                    instrument=instrument
                ).delete()
            except FavoriteInstrument.DoesNotExist:
                pass

        try:
            favorite_instrument = FavoriteInstrument.objects.get(
                user_id=request.user.id,
                instrument=instrument
            )

            position = int(self.request.DATA.get('favorite_position'))
            #Change current instrument position based on DATA from POST
            favorite_instrument.move_to(position)

        except (ValueError, TypeError, FavoriteInstrument.DoesNotExist):
            pass

        data = self.get_serializer_class()(
            self.get_object(),
            context={'request': self.request}
        ).data

        return Response(data=data, status=status.HTTP_200_OK)

    #Prevent create or destroy object
    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_400_BAD_REQUEST)

    def destroy(self, request, *args, **kwargs):
        return Response(status=status.HTTP_400_BAD_REQUEST)


class ModelViewSetStripped(#mixins.CreateModelMixin,
                    mixins.RetrieveModelMixin,
                    #mixins.UpdateModelMixin,
                    #mixins.DestroyModelMixin,
                    mixins.ListModelMixin,
                    viewsets.GenericViewSet):
    """
    A viewset that provides default `create()`, `retrieve()`, `update()`,
    `partial_update()`, `destroy()` and `list()` actions.
    """
    pass


#region Positions
class OpenPositionsViewSet(ModelViewSetStripped):
    serializer_class = PositionSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    def get_queryset(self):
        return AccountService(self.request.user).user_current_open_positions()


class ClosedPositionsViewSet(viewsets.ModelViewSet):
    serializer_class = PositionSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    paginate_by = 10

    def get_queryset(self):
        return AccountService(self.request.user).user_history_by_positions()


class CreatePositionViewSet(viewsets.GenericViewSet):
    serializer_class = PositionCreateSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly, )

    def create(self, request):
        serializer = PositionCreateSerializer(data=request.DATA)
        if serializer.is_valid():
            object = serializer.save()
            object['user'] = request.user
            try:
                position_id = TradeService.open_position(**object)
                result = {'position_id':position_id}
                result.update(serializer.data)
            except InstrumentNotTradeable:
                raise InstrumentNotTradeableApi
            except Overdraft:
                raise OverdraftApi
            except WrongAmount:
                instrument = object['instrument']
                raise WrongOpeningAmountApi(instrument.trade_size_increment, instrument.min_trade_size)
            return Response(result, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ClosePositionViewSet(viewsets.GenericViewSet):
    serializer_class = PositionCloseSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly, )

    def create(self, request):
        serializer = PositionCloseSerializer(data=request.DATA)
        if serializer.is_valid():
            object = serializer.save()
            try:
                TradeService.close_position(**object)
                result = serializer.data
                result.update({'last_client_trade':object['position'].clienttrade_set.order_by('-time')[0].id})
            except WrongAmount:
                raise WrongAmountApi
            return Response(result, status=status.HTTP_202_ACCEPTED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
#endregion Positions


#region Orders
class PlaceOrderViewSet(viewsets.GenericViewSet):
    serializer_class = PlaceOrderSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly, )

    def create(self, request):
        serializer = PlaceOrderSerializer(data=request.DATA)
        if serializer.is_valid():
            object = serializer.save()
            object['user'] = request.user
            order_id = TradeService.place_conditional_order(**object)
            result = {'order_id': order_id}
            result.update(serializer.data)
            return Response(result, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CancelOrderViewSet(viewsets.GenericViewSet):
    serializer_class = CancelOrderSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly, )

    def create(self, request):
        serializer = CancelOrderSerializer(data=request.DATA)
        if serializer.is_valid():
            object = serializer.save()
            TradeService.cancel_order(**object)
            result = serializer.data
            return Response(result, status=status.HTTP_202_ACCEPTED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PendingOrdersViewSet(ModelViewSetStripped):
    serializer_class = OrderSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    def get_queryset(self):
        return AccountService(self.request.user).user_current_pending_orders()
#endregion Orders


class RequiredMarginViewSet(viewsets.GenericViewSet):
    serializer_class = RequiredMarginSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly, )

    def create(self, request):
        serializer = RequiredMarginSerializer(data=request.DATA)
        if serializer.is_valid():
            object = serializer.save()
            object['stop_loss_rate'] = object['stop_loss_distance']
            del object['stop_loss_distance']
            margin = TradeService._calculate_margin(**object)
            return Response(margin, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TradesViewSet(viewsets.ModelViewSet):
    serializer_class = ClientTradeSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    def get_queryset(self):
        return ClientTrade.objects.filter(user=self.request.user, success=True)



