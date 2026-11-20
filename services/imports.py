import csv
import io
import secrets
import time
from flask import abort, flash, g, redirect, render_template, request, session, url_for, Response
from contract import REQUIRED, dumps, load_json, normalize_record
from services.endpoints import ApiError, sample
from services.catalog import check_record, resolve_condition

MAX_ROWS=50

def parse_file(filename, raw):
    if len(raw)>262144: raise ValueError('File exceeds 256 KB.')
    try: text=raw.decode('utf-8-sig')
    except UnicodeDecodeError: raise ValueError('Use a UTF-8 encoded file.') from None
    if filename.lower().endswith('.csv'):
        reader=csv.DictReader(io.StringIO(text), strict=True)
        names=reader.fieldnames or []
        if len(names)!=len(set(names)): raise ValueError('Duplicate CSV column names.')
        rows=list(reader)
    elif filename.lower().endswith('.json'):
        rows=load_json(text)
        if not isinstance(rows,list): raise ValueError('JSON must contain an array of records.')
    else: raise ValueError('Only CSV and JSON files are accepted.')
    if not 1<=len(rows)<=MAX_ROWS: raise ValueError('Upload between 1 and 50 records.')
    result=[]; seen=set()
    for number,row in enumerate(rows,1):
        entry={'number':number,'record':{},'error':'','result':''}
        try:
            record=normalize_record(row,allow_text=filename.lower().endswith('.csv'))
            record.pop('record_id',None)
            key=(record['location_id'],record['timestamp_utc'])
            if key in seen: raise ValueError('Duplicate location and UTC minute in this file.')
            seen.add(key)
            entry['record']=record
        except (ValueError,TypeError) as exc:
            entry['error']=str(exc) if isinstance(exc,ValueError) else 'Invalid record structure.'
        result.append(entry)
    return result

def apply_catalog(rows, catalog):
    """Calculate AUTO conditions and check IDs against the MySQL catalog (AWS mode)."""
    for entry in rows:
        if entry['error']: continue
        try: entry['record']=check_record(resolve_condition(entry['record']),catalog)
        except ValueError as exc: entry['error']=str(exc)
    return rows

def register_imports(app,store,client,scope,auth=None):
    auth=auth or store
    @app.route('/import',methods=['GET','POST'])
    def import_records():
        if request.method=='POST':
            file=request.files.get('file')
            try:
                if not file or not file.filename: raise ValueError('Choose a CSV or JSON file.')
                rows=apply_catalog(parse_file(file.filename,file.stream.read(262145)),auth.catalog())
                job=secrets.token_urlsafe(24)
                with store.connect() as db:
                    db.execute('DELETE FROM imports WHERE created<?',(time.time()-86400,))
                    db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?)',(job,g.user['username'],scope,time.time(),'preview',dumps(rows)))
                return redirect(url_for('import_preview',job=job))
            except (ValueError,csv.Error) as exc: flash(str(exc),'error')
        return render_template('import.html')

    def get_job(job):
        with store.connect() as db:
            row=db.execute('SELECT * FROM imports WHERE id=? AND owner=? AND scope=?',(job,g.user['username'],scope)).fetchone()
        if not row or time.time()-row['created']>3600: abort(404,'Import expired. Upload the file again.')
        return row,load_json(row['payload'])

    @app.get('/import/<job>')
    def import_preview(job):
        saved,rows=get_job(job)
        return render_template('import_preview.html',job=job,rows=rows,state=saved['state'],valid=not any(r['error'] for r in rows))

    @app.post('/import/<job>/confirm')
    def import_confirm(job):
        saved,rows=get_job(job)
        if any(r['error'] for r in rows): abort(400,'Fix all validation errors before importing.')
        with store.connect() as db:
            claimed=db.execute("UPDATE imports SET state='processing' WHERE id=? AND state='preview'",(job,)).rowcount
        if not claimed: return redirect(url_for('import_preview',job=job))
        created=[]
        for row in rows:
            entity=row['record']['location_id']+'|'+row['record']['timestamp_utc']
            try:
                result=client.create(row['record'])
                row['result']='Imported'
                if result.get('record'): created.append(result['record'])
                app.extensions['audit']('READING_IMPORT','READING',entity,'SUCCESS',after=result.get('record'))
            except ApiError as exc:
                row['result']='Not confirmed: '+str(exc)+' Check the record before retrying.'
                app.extensions['audit']('READING_IMPORT','READING',entity,'REJECTED',after=row['record'],reason=str(exc))
            # Save after each response; never automatically retry an uncertain write.
            with store.connect() as db:
                db.execute('UPDATE imports SET payload=? WHERE id=?',(dumps(rows),job))
        with store.connect() as db: db.execute("UPDATE imports SET state='completed' WHERE id=?",(job,))
        if created: app.extensions['analytics_sync'](created,'importacion')   # SP sp_upsert_lecturas
        return redirect(url_for('import_preview',job=job))

    @app.get('/import/template/<kind>')
    def import_template(kind):
        row=sample(); row['timestamp_utc']='2026-10-10T06:00:00Z'
        catalog=auth.catalog()
        if catalog:
            row.update(condition_id='AUTO',
                       source_id='SRC-MANUAL' if 'SRC-MANUAL' in catalog['source_ids'] else sorted(catalog['source_ids'])[0])
        if kind=='json': data=dumps([row]); mime='application/json'
        elif kind=='csv':
            stream=io.StringIO(newline=''); writer=csv.DictWriter(stream,fieldnames=sorted(REQUIRED));writer.writeheader();writer.writerow(row)
            data=stream.getvalue();mime='text/csv'
        else: abort(404)
        return Response(data,mimetype=mime,headers={'Content-Disposition':f'attachment; filename="climate_template.{kind}"'})
