from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.conf import settings
from django.http import HttpResponse
import mongoengine
from mongoengine import Q

from models import Position
from mongo_models import ChartHistory
from service import TradeService
from forms import OpenPositionForm
from accounts.service import AccountService

from rest_framework.renderers import JSONRenderer


@login_required
def create_position(request):
    if request.method == 'POST':
        form = OpenPositionForm(request.POST)
        if form.is_valid():
            instrument = form.cleaned_data['instrument']
            asked_rate = form.cleaned_data['asked_rate']
            amount = form.cleaned_data['amount']
            side = form.cleaned_data['side']
            stop_loss = form.cleaned_data['stop_loss']
            take_profit = form.cleaned_data['take_profit']
            TradeService.open_position(request.user, instrument, asked_rate, amount, side, stop_loss, take_profit)
    else:
        form = OpenPositionForm()

    return render(request, 'positions/create_position.html', locals())

@login_required
def positions_list(request):
    open_positions_list = AccountService(request.user).user_current_open_positions()
    closed_position_list = AccountService(request.user).user_history_by_positions()
    return render(request, 'positions/position_lists.html', locals())

@login_required
def close_position(request, pk):
    p = Position.objects.get(pk=pk)
    TradeService.close_position(p, p.amount)
    return redirect('positions-list')


def serialize_history(qs):
    result = []
    for obj in qs:
        row = [
            obj.date.strftime('%Y-%m-%d %H:%M'),
            obj.open,
            obj.high,
            obj.low,
            obj.close,
            0
        ]
        result.append(row)

    return JSONRenderer().render(result)


def chartiq(request):
    mongoengine.connect(**settings.MONGO_DATABASES['chart_history'])

    context = {
        'data': [],
        'data_intraday': []
    }

    if request.GET.get('symbol'):
        symbol = request.GET.get('symbol', '')
        period = request.GET.get('period', 'DAILY')
        type = getattr(ChartHistory, 'TYPE_%s' % period, ChartHistory.TYPE_DAILY)

        context['symbol'] = symbol

        context['data'] = serialize_history(ChartHistory.objects.filter(
            instrument=symbol,
            type=ChartHistory.TYPE_DAILY
        ).order_by('-date')[:100])

        context['data_intraday'] = serialize_history(ChartHistory.objects.filter(
            instrument=symbol,
            type=ChartHistory.TYPE_M1
        ).order_by('-date')[:100])

        # Partials, now not used
        if request.is_ajax() and request.GET.get('period'):
            return HttpResponse(data=context['data'], mimetype='application/json')

    return render(request, 'trade/chartiq.html', context)