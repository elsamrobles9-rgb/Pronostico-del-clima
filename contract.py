"""Shared English v2 contract. No network calls or AWS dependency."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

# Compatibility is confined to migration; new clients must use English keys.
LEGACY_KEYS = {
 'id_ubicacion':'location_id','fecha_hora':'timestamp_utc',
 'id_registro':'record_id','id_condicion':'condition_id',
 'id_estacion':'station_id','id_fuente':'source_id',
 'temperatura':'temperature_c','sensacion_termica':'feels_like_c',
 'humedad':'humidity_pct','presion_atmosferica':'pressure_hpa',
 'velocidad_viento':'wind_speed_kmh','direccion_viento':'wind_direction',
 'precipitacion':'precipitation_mm','nubosidad':'cloud_cover_pct',
 'visibilidad':'visibility_km','indice_uv':'uv_index','punto_rocio':'dew_point_c',
 'periodo_precipitacion_min':'precipitation_period_minutes',
}
NUMBER_RULES = {
 'temperature_c':(-100,70), 'feels_like_c':(-120,100),
 'humidity_pct':(0,100),'pressure_hpa':(100,1200),
 'wind_speed_kmh':(0,500),'precipitation_mm':(0,5000),
 'cloud_cover_pct':(0,100),'visibility_km':(0,1000),
 'uv_index':(0,None),'dew_point_c':(-120,70),
}
# Upper/lower meteorological limits are provisional, inherited from the project.
IDS = ('location_id','station_id','source_id','condition_id')
REQUIRED = set(IDS) | set(NUMBER_RULES) | {
 'timestamp_utc','wind_direction','precipitation_period_minutes'}
ALLOWED = REQUIRED | {'record_id'}
WIND = {'N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'}
WIND_ALIASES = {'O':'W','SO':'SW','NO':'NW','SSO':'SSW','OSO':'WSW','ONO':'WNW','NNO':'NNW'}

def reject_constant(value):
    raise ValueError('Non-finite JSON numbers are not allowed.')

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON field: {key}')
        result[key] = value
    return result

def load_json(text):
    return json.loads(text,parse_int=Decimal,parse_float=Decimal,
                      parse_constant=reject_constant,object_pairs_hook=unique_object)

def decimal_text(number):
    if not number.is_finite():
        raise ValueError('Non-finite number.')
    if number == 0:
        return '0'
    return format(number.normalize(),'f')

def dumps(value):
    # Exact Decimal JSON encoding; no conversion through float.
    if isinstance(value,Decimal):
        return decimal_text(value)
    if isinstance(value,dict):
        return '{'+','.join(json.dumps(k)+':'+dumps(value[k]) for k in sorted(value))+'}'
    if isinstance(value,(tuple,list)):
        return '['+','.join(dumps(v) for v in value)+']'
    return json.dumps(value,ensure_ascii=False,allow_nan=False)

def normalize_time(value, legacy_offset=None):
    if not isinstance(value,str) or not re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?',value):
        raise ValueError('Timestamp must use ISO 8601 with an explicit UTC offset.')
    offset = re.search(r'([+-])(\d{2}):(\d{2})$',value)
    if offset and (int(offset[2])>14 or int(offset[3])>59 or (int(offset[2])==14 and int(offset[3])!=0)):
        raise ValueError('Invalid UTC offset.')
    dt = datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:
        if not legacy_offset or not re.fullmatch(r'[+-]\d{2}:\d{2}',legacy_offset):
            raise ValueError('Timezone missing. Confirm the historical offset before migration.')
        hours,minutes = map(int,legacy_offset[1:].split(':'))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise ValueError('Invalid UTC offset.')
        delta = timedelta(hours=hours,minutes=minutes)
        dt = dt.replace(tzinfo=timezone(delta if legacy_offset[0]=='+' else -delta))
    if abs(dt.utcoffset()) > timedelta(hours=14):
        raise ValueError('Invalid UTC offset.')
    return dt.astimezone(timezone.utc).replace(second=0,microsecond=0).strftime('%Y-%m-%dT%H:%M:00Z')

def number(value,label,allow_text=False):
    if isinstance(value,bool) or not isinstance(value,(int,float,Decimal,str) if allow_text else (int,float,Decimal)):
        raise ValueError(f'{label} must be a number.')
    if len(str(value))>64:
        raise ValueError(f'{label} is too long.')
    try:
        result = Decimal(str(value))
    except (InvalidOperation,ValueError):
        raise ValueError(f'{label} must be a number.')
    if not result.is_finite() or result.as_tuple().exponent < -6:
        raise ValueError(f'{label} must be finite with at most six decimal places.')
    return result

def normalize_record(data, *, legacy=False, legacy_offset=None, allow_text=False,
                     station_id=None, source_id=None, precipitation_period_minutes=None):
    if not isinstance(data,dict):
        raise ValueError('Expected one record object.')
    translated = {}
    for key,value in data.items():
        name = LEGACY_KEYS.get(key,key) if legacy else key
        if name in translated:
            raise ValueError(f'Conflicting legacy and English field: {name}')
        translated[name] = value
    for name,value in [('station_id',station_id),('source_id',source_id),
                       ('precipitation_period_minutes',precipitation_period_minutes)]:
        if name not in translated and value is not None:
            translated[name] = value
    extra = set(translated)-ALLOWED
    if extra:
        raise ValueError('Unknown fields: '+', '.join(sorted(extra)))
    missing = REQUIRED-set(translated)
    if missing:
        raise ValueError('Missing fields: '+', '.join(sorted(missing)))
    result = {}
    for name in IDS:
        value = translated[name]
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):
            raise ValueError(f'{name} must contain 1-64 letters, digits, underscores or hyphens.')
        result[name]=value
    result['timestamp_utc']=normalize_time(translated['timestamp_utc'],legacy_offset)
    for name,(low,high) in NUMBER_RULES.items():
        value = number(translated[name],name,allow_text)
        if abs(value) > Decimal('999999.999999') or value < low or (high is not None and value > high):
            raise ValueError(f'{name} is outside its configured range.')
        result[name]=value
    if result['dew_point_c'] > result['temperature_c']:
        raise ValueError('Dew point exceeds temperature (zero-tolerance rule pending approval).')
    wind = translated['wind_direction']
    if not isinstance(wind,str):
        raise ValueError('Wind direction must be text.')
    wind=WIND_ALIASES.get(wind.strip().upper(),wind.strip().upper())
    if wind not in WIND:
        raise ValueError('Invalid wind direction.')
    result['wind_direction']=wind
    period=number(translated['precipitation_period_minutes'],'precipitation_period_minutes',allow_text)
    if period != period.to_integral_value() or not 1 <= period <= 1440:
        raise ValueError('Precipitation period must be an integer between 1 and 1440 minutes.')
    result['precipitation_period_minutes']=int(period)
    record_id=translated.get('record_id')
    if record_id is None:
        record_id=str(uuid.uuid4())
    if not isinstance(record_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',record_id):
        raise ValueError('Invalid record ID.')
    result['record_id']=record_id
    return result

def content_hash(record):
    content={k:v for k,v in record.items() if k!='record_id'}
    return hashlib.sha256(dumps(content).encode()).hexdigest()

def to_attribute(value):
    if value is None: return {'NULL':True}
    if isinstance(value,bool): return {'BOOL':value}
    if isinstance(value,str): return {'S':value}
    if isinstance(value,(Decimal,int)): return {'N':decimal_text(Decimal(value))}
    if isinstance(value,list): return {'L':[to_attribute(v) for v in value]}
    if isinstance(value,dict): return {'M':{k:to_attribute(v) for k,v in value.items()}}
    raise ValueError('Unsupported DynamoDB value.')

def from_attribute(value):
    if not isinstance(value,dict) or len(value)!=1:
        raise ValueError('Invalid DynamoDB attribute.')
    kind,data=next(iter(value.items()))
    if kind=='S': return data
    if kind=='N': return Decimal(data)
    if kind=='NULL': return None
    if kind=='BOOL': return data
    if kind=='M': return {k:from_attribute(v) for k,v in data.items()}
    if kind=='L': return [from_attribute(v) for v in data]
    raise ValueError(f'Unsupported DynamoDB type: {kind}')
