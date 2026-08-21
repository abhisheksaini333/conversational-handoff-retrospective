"""Calibrate on development examples; evaluate held-out synthetic utterances once."""
import hashlib
import math
import json
import platform
import statistics
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.integration_demo import request

ROOT=Path(__file__).resolve().parents[1]
DEVELOPMENT=[
 ('hello there','greet',False),('good afternoon','greet',False),
 ('can you check my parcel','order_status',False),('what is the status of my shipment','order_status',False),
 ('a human agent please','request_human',True),('I need to talk to a person','request_human',True),
 ('please explain orbital mechanics','out_of_scope',True),('buy me a banana submarine','out_of_scope',True)]
HELD_OUT=[
 ('hey support team','greet',False),('hello good evening','greet',False),
 ('is my package on the way','order_status',False),('track the shipment for me','order_status',False),
 ('speak to a support representative','request_human',True),('please connect me with someone','request_human',True),
 ('solve my quantum physics homework','out_of_scope',True),('compose a song about volcanoes','out_of_scope',True)]

def parse(rows):
    outputs=[]
    for text,label,human in rows:
        start=time.perf_counter()
        result=request('/model/parse',{'text':text},'rasa')
        duration=(time.perf_counter()-start)*1000
        ranking=[x for x in result.get('intent_ranking',[]) if x['name']!='nlu_fallback']
        top=ranking[0] if ranking else {'name':'nlu_fallback','confidence':0}
        outputs.append({'text':text,'label':label,'needs_human':human,'prediction':top['name'],'confidence':top['confidence'],'latency_ms':duration})
    return outputs

def score(rows,threshold):
    if type(threshold) not in (int,float) or not math.isfinite(threshold) or not 0 <= threshold <= 1 or not isinstance(rows,list) or not rows:
        raise ValueError("routing evaluation requires rows and a finite threshold in [0,1]")
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get("prediction"),str) or not row["prediction"].strip() or type(row.get("needs_human")) is not bool or type(row.get("confidence")) not in (int,float) or not math.isfinite(row["confidence"]) or not 0 <= row["confidence"] <= 1:
            raise ValueError("invalid routing evaluation sample")
    false=missed=correct=0
    for row in rows:
        handoff=row['prediction']=='request_human' or row['confidence']<threshold
        false+=int(handoff and not row['needs_human'])
        missed+=int(not handoff and row['needs_human'])
        correct+=int(handoff==row['needs_human'])
    return {'false_handoffs':false,'missed_handoffs':missed,'correct_routing_rate':correct/len(rows),'count':len(rows)}

def main():
    dev=parse(DEVELOPMENT)
    thresholds=[.3,.4,.5,.6,.7,.8,.9]
    # Penalize a missed handoff twice as much as an unnecessary handoff.
    chosen=min(thresholds,key=lambda t:(2*score(dev,t)['missed_handoffs']+score(dev,t)['false_handoffs'],t))
    held=parse(HELD_OUT)
    labels=sorted({x['label'] for x in held})
    f1=[]
    for label in labels:
        tp=sum(x['label']==label and x['prediction']==label for x in held)
        fp=sum(x['label']!=label and x['prediction']==label for x in held)
        fn=sum(x['label']==label and x['prediction']!=label for x in held)
        f1.append(2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0)
    latencies=sorted(x['latency_ms'] for x in held)
    report={'executed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'hardware':platform.platform(),
            'dataset_sha256':hashlib.sha256(json.dumps([DEVELOPMENT,HELD_OUT]).encode()).hexdigest(),
            'baseline_threshold':.3,'calibrated_threshold':chosen,'baseline':score(held,.3),'calibrated':score(held,chosen),
            'deployed_threshold':.6,'deployed':score(held,.6),
            'intent_macro_f1_including_out_of_scope':statistics.mean(f1),'latency_ms':{'p50':statistics.median(latencies),'p95':latencies[-1]},
            'development':dev,'held_out':held,'limitations':['16 synthetic utterances only; not a representative benchmark.','Threshold comparison is offline; deployed Rasa threshold remains 0.6.','Rasa has no trained out_of_scope class; its macro F1 includes those errors.','Classifier routing quality is separate from protocol task-completion checks.','p95 is nearest-rank across 8 held-out requests; includes localhost network overhead.']}
    (ROOT/'evidence/evaluation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ['baseline','calibrated','calibrated_threshold','intent_macro_f1_including_out_of_scope','latency_ms']},indent=2))
if __name__=='__main__':main()
