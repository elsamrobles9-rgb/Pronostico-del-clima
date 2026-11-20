"""English v2 CRUD service. Inject a DynamoDB Table; never reads AWS credentials."""
import base64
import json
import re
import uuid
from datetime import datetime,timezone
from decimal import Decimal
from contract import normalize_record,normalize_time,load_json,dumps,content_hash,ALLOWED

KEYS={'location_id','timestamp_utc'}

class RequestError(ValueError):
    def __init__(self,status,message):
        self.status=status
        super().__init__(message)

def respond(status,data):
    return {'statusCode':status,'headers':{'Content-Type':'application/json'},'body':dumps(data)}

def parse_body(event):
    raw=event.get('body')
    if not isinstance(raw,str) or not raw:
        raise RequestError(400,'A JSON body is required.')
    if len(raw.encode())>24000:
        raise RequestError(413,'Request body is too large.')
    if event.get('isBase64Encoded'):
        try: raw=base64.b64decode(raw,validate=True).decode('utf-8')
        except (ValueError,UnicodeError):raise RequestError(400,'Invalid body encoding.')
    if len(raw.encode())>16000:
        raise RequestError(413,'Request body is too large.')
    try: data=load_json(raw)
    except (ValueError,RecursionError):raise RequestError(400,'Invalid JSON body.')
    if not isinstance(data,dict):raise RequestError(400,'Expected a JSON object.')
    return data

def location(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):
        raise RequestError(400,'Invalid location ID.')
    return value

def keys(data):
    return {'location_id':location(data.get('location_id')),
            'timestamp_utc':normalize_time(data.get('timestamp_utc'))}

def expected_version(data):
    value=data.get('expected_version')
    if isinstance(value,bool) or not isinstance(value,(int,Decimal)) or value!=int(value) or value<1:
        raise RequestError(400,'A positive integer expected_version is required.')
    return int(value)

def decode_cursor(token):
    try:
        if not isinstance(token,str) or len(token)>4096:raise ValueError()
        value=load_json(base64.b64decode(token,altchars=b'-_',validate=True).decode())
        if not isinstance(value,dict) or set(value)!=KEYS:raise ValueError()
        if keys(value)!=value:raise ValueError()
        return value
    except (ValueError,TypeError,UnicodeError):
        raise RequestError(400,'Invalid cursor.')

def now():return datetime.now(timezone.utc).isoformat()

class ReadingService:
    def __init__(self,table):self.table=table

    def get(self,key):return self.table.get_item(Key=key,ConsistentRead=True).get('Item')

    def save(self,item,old=None):
        options={'Item':item}
        if old is None:
            options['ConditionExpression']='attribute_not_exists(location_id) AND attribute_not_exists(timestamp_utc)'
        else:
            options.update(ConditionExpression='attribute_exists(location_id) AND #v = :v',
                           ExpressionAttributeNames={'#v':'version'},
                           ExpressionAttributeValues={':v':old['version']})
        self.table.put_item(**options)

    def write(self,data,operation):
        key=keys(data)
        extra=set(data)-(ALLOWED|{'expected_version'})
        if extra:raise RequestError(400,'Unknown fields: '+', '.join(sorted(extra)))
        old=self.get(key)
        payload={k:v for k,v in data.items() if k!='expected_version'}
        if operation=='update':
            if old is None:raise RequestError(404,'Record not found.')
            version=expected_version(data)
            if version!=old['version']:raise RequestError(409,'Record version conflict. Reload the record.')
            if set(payload)<=KEYS:raise RequestError(400,'Provide at least one field to update.')
            if 'record_id' in payload and payload['record_id']!=old['record_id']:
                raise RequestError(400,'Record ID cannot be changed.')
            payload={**{k:v for k,v in old.items() if k in ALLOWED},**payload}
        normalized=normalize_record(payload)
        if old:
            if normalized['station_id']!=old['station_id']:
                raise RequestError(409,'Station cannot change for an existing location/minute.')
            if 'record_id' in data and data['record_id']!=old['record_id']:
                raise RequestError(400,'Record ID cannot be changed.')
            normalized['record_id']=old['record_id']
        digest=content_hash(normalized)
        if old and digest==old['content_hash']:
            raise RequestError(409,'The record already contains the same information.')
        if old and operation=='create':
            version=expected_version(data)
            if version!=old['version']:raise RequestError(409,'Record version conflict. Reload the record.')
        if old is None and 'expected_version' in data:
            raise RequestError(409,'Cannot update a missing record with expected_version.')
        timestamp=now()
        item={**normalized,'version':int(old['version'])+1 if old else 1,
              'content_hash':digest,'schema_version':2,
              'created_at_utc':old['created_at_utc'] if old else timestamp,
              'updated_at_utc':timestamp,'transaction_id':str(uuid.uuid4()),
              'condition_origin':'SOURCE'}
        self.save(item,old)
        return respond(200 if old else 201,{'message':'Record updated.' if old else 'Record created.','record':item})

    def delete(self,data):
        if set(data)!=(KEYS|{'expected_version'}):
            raise RequestError(400,'Send only location_id, timestamp_utc and expected_version.')
        key=keys(data);version=expected_version(data)
        old=self.get(key)
        if old is None:raise RequestError(404,'Record not found.')
        if old['version']!=version:raise RequestError(409,'Record version conflict. Reload the record.')
        self.table.delete_item(Key=key,ConditionExpression='attribute_exists(location_id) AND #v = :v',
            ExpressionAttributeNames={'#v':'version'},ExpressionAttributeValues={':v':version})
        return respond(200,{'message':'Record deleted.','record':old})

    def read(self,event):
        params=event.get('queryStringParameters') or {}
        if not isinstance(params,dict):raise RequestError(400,'Invalid query parameters.')
        if params.get('list')=='1':
            if set(params)-{'list','location_id','cursor'}:raise RequestError(400,'Unknown query parameters.')
            options={'Limit':25,'ConsistentRead':True}
            if params.get('cursor'):options['ExclusiveStartKey']=decode_cursor(params['cursor'])
            loc=params.get('location_id')
            if loc:
                loc=location(loc)
                if options.get('ExclusiveStartKey',{}).get('location_id',loc)!=loc:
                    raise RequestError(400,'Cursor does not match location.')
                result=self.table.query(KeyConditionExpression='#loc = :loc',
                    ExpressionAttributeNames={'#loc':'location_id'},
                    ExpressionAttributeValues={':loc':loc},**options)
            else:result=self.table.scan(**options)
            next_key=result.get('LastEvaluatedKey')
            token=base64.urlsafe_b64encode(dumps(next_key).encode()).decode() if next_key else ''
            return respond(200,{'items':result.get('Items',[]),'next_cursor':token})
        data=params if params else parse_body(event)
        if set(data)!=KEYS:raise RequestError(400,'Send only location_id and timestamp_utc.')
        item=self.get(keys(data))
        if item is None:raise RequestError(404,'Record not found.')
        return respond(200,item)

    def handle(self,operation,event):
        try:
            if not isinstance(event,dict):raise RequestError(400,'Invalid event.')
            method=event.get('requestContext',{}).get('http',{}).get('method') or event.get('httpMethod')
            expected={'read':'GET','create':'POST','update':'PUT','delete':'DELETE'}[operation]
            if method and method!=expected:raise RequestError(405,'Method not allowed.')
            if operation=='read':return self.read(event)
            data=parse_body(event)
            if operation=='delete':return self.delete(data)
            return self.write(data,operation)
        except RequestError as error:return respond(error.status,{'message':str(error)})
        except (ValueError,TypeError):return respond(400,{'message':'Invalid record. Check required fields, timestamp and measurements.'})
        except Exception as error:
            code=getattr(error,'response',{}).get('Error',{}).get('Code')
            if code=='ConditionalCheckFailedException':return respond(409,{'message':'Concurrent modification. Reload and retry.'})
            # Never log credentials, complete payloads or raw exception messages.
            print('Reading operation failed:',type(error).__name__)
            return respond(500,{'message':'Could not complete the operation.'})
