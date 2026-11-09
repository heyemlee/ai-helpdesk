import hashlib,json,sqlite3

def configure(runner,root,metadata):
    specs=[('production_blocker','J29-90001','What is preventing order J29-90001 from shipping?','Waiting for fastener reinspection',None),
           ('delivery_blocker','J29-90002','订单 J29-90002 为什么还没有发货？',None,'Temporary road closure prevents unloading access'),
           ('unknown_cause','J29-90003','Why is shipment of J29-90003 delayed?',None,None)]
    for db in runner.pipeline.databases:
        if db.name not in ('erp_db','prod_db','svc_db'):continue
        table={'erp_db':'orders','prod_db':'work_order','svc_db':'delivery'}[db.name]
        with sqlite3.connect(db.db_path) as conn:
            cursor=conn.execute(f"SELECT * FROM {table} WHERE order_no='J25-00094' LIMIT 1")
            columns=[c[0] for c in cursor.description];template=dict(zip(columns,cursor.fetchone()))
            for ident,order,query,production,delivery in specs:
                row={**template,'order_no':order}
                if db.name=='erp_db':row.update(order_notes=None,order_status='生产中' if production else '待发货')
                if db.name=='prod_db':row.update(production_notes=production,production_status='生产中' if production else '已完工')
                if db.name=='svc_db':row.update(delivery_notes=delivery)
                conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",[row[c] for c in columns])
    items=[]
    for ident,order,query,production,delivery in specs:
        alias,table,column=('prod','work_order','production_notes') if production or not delivery else ('svc','delivery','delivery_notes')
        items.append({'id':ident,'category':'customer','query':query,'gold_sql':f"SELECT {column} FROM {alias}.{table} WHERE order_no='{order}'",'gold_columns':[], 'expected_notes':{'production_notes':production,'delivery_notes':delivery}})
    runner.dataset=items
    payload=json.dumps(items,ensure_ascii=False,indent=2)
    (root/'control-dataset.json').write_text(payload)
    metadata['control_dataset_sha256']=hashlib.sha256(payload.encode()).hexdigest()

def check(item,evidence):
    observed={}
    for step in evidence['steps']:
        for row in step['execution_result'].get('rows',[]):
            for column in item['expected_notes']:
                if column in row:observed.setdefault(column,[]).append(row[column])
    passed=all(observed.get(k)==[v] for k,v in item['expected_notes'].items())
    return {'passed':passed,'expected':item['expected_notes'],'observed':observed}
