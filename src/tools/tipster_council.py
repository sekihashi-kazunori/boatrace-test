"""予想家会議の検証(2026-11-21 ご主人様「色々な予想師が居る。色々な角度で検証して掘り下げて高精度な予想を出していく」)。

みなもさん・みなとさん・公式PC・あそかさん・しねん・執事の「1番目の予想」を、結果の分かる全レースで比べ、
頭の一致数・3艇の一致数ごとの的中率と回収率を出す。    python -m src.tools.tipster_council
"""
import csv,json,glob,os,re,collections,sqlite3
RES={};TRIO={}
for f in glob.glob('odds_hist2/*.csv'):
    d=os.path.basename(f)[:-4]
    for r in csv.DictReader(open(f)):
        try:p=json.loads(r['payouts'])
        except:continue
        k=(d,r['stadium_code'].zfill(2),int(r['race_number']))
        if p.get('trifecta'):RES[k]=(p['trifecta'][0]['combination'],p['trifecta'][0]['payout'])
        if p.get('trio'):TRIO[k]=p['trio'][0]['payout']
for f in glob.glob('plans/day_results/*.csv'):
    d=os.path.basename(f)[:-4]
    for r in csv.DictReader(open(f)):RES.setdefault((d,r['stadium_code'],int(r['race_number'])),(r['trifecta'],int(r['payout'])))
S=collections.defaultdict(dict)  # src -> k -> (ranked combos list, buy combos)
for f in glob.glob('tipster/minamo_all_*.csv'):
    for r in csv.DictReader(open(f)):
        cs=r['combos'].split();S['みなも'][(r['race_date'],r['stadium_code'],int(r['race_number']))]=(cs,cs)
for f in glob.glob('tipster/minato_*.csv'):
    for r in csv.DictReader(open(f)):
        cs=r['combos'].split();S['みなと'][(r['race_date'],r['stadium_code'],int(r['race_number']))]=(cs,cs)
for f in glob.glob('tipster/pc_*.csv'):
    for r in csv.DictReader(open(f)):
        cs=r['combos'].split();S['公式PC'][(r['race_date'],r['stadium_code'],int(r['race_number']))]=(cs,cs)
for f in glob.glob('asoka/*.csv'):
    d=os.path.basename(f)[:-4]
    for r in csv.DictReader(open(f)):
        cs=r['combo'].split();S['あそか'][(d,r['stadium_code'].zfill(2),int(r['race_number']))]=(cs,cs)
for f in glob.glob('shinen/day_*.json'):
    j=json.load(open(f));d=j['date'];d=f'{d[:4]}-{d[4:6]}-{d[6:]}'
    for kk,r in j['races'].items():
        top=[x['c'] for x in r.get('top',[])]
        if top:S['しねん'][(d,str(r['venue_code']).zfill(2),int(r['race_num']))]=(top,top[:2])
c=sqlite3.connect('state.db')
for d,s,r,pj in c.execute('select race_date,stadium_code,race_number,probs_json from odds_snapshots'):
    try:p=json.loads(pj)
    except:continue
    top=sorted(p,key=p.get,reverse=True);S['執事'][(d,s,r)]=(top,top[:2])
print('== 予想家ごと(結果が分かるレース)')
print('名前 R数 | 1番目の頭が1着 | 1番目の3艇で3連複 | 1番目の1-2着 | 自分の買い目の的中率・回収率')
for src,m in S.items():
    n=h=q=e=hb=I=R=0;qr=0
    for k,(rank,buy) in m.items():
        if k not in RES:continue
        t,p=RES[k];tt=t.split('-');a=rank[0].split('-');n+=1
        h+=tt[0]==a[0];q+=set(tt)==set(a);e+=tt[:2]==a[:2]
        qr+=TRIO.get(k,0) if set(tt)==set(a) else 0
        I+=100*len(buy);R+=p if t in buy else 0;hb+=t in buy
    if n: print(f'{src} {n}R | {h/n*100:.1f}% | {q/n*100:.1f}%(回収{qr/n:.0f}%) | {e/n*100:.1f}% | {len(next(iter(m.values()))[1])}点 的中{hb/n*100:.1f}% 回収{R/I*100:.1f}%')
# consensus head
print('== 頭(1着)の一致数と1着率')
srcs=list(S)
A=collections.defaultdict(lambda:[0,0]);B=collections.defaultdict(lambda:[0,0,0])
keys=set().union(*[set(m) for m in S.values()])
for k in keys:
    if k not in RES:continue
    t=RES[k][0].split('-')
    heads=[S[s][k][0][0].split('-')[0] for s in srcs if k in S[s]]
    if len(heads)<3:continue
    v=collections.Counter(heads);top,cnt=v.most_common(1)[0]
    frac=cnt/len(heads)
    key=f'{len(heads)}人中{cnt}人一致'
    A[key][0]+=1;A[key][1]+=t[0]==top
    # trio consensus: vote boats appearing in each source's top1 combo
    bv=collections.Counter()
    for s in srcs:
        if k in S[s]:bv.update(S[s][k][0][0].split('-'))
    trio=sorted([b for b,_ in bv.most_common(3)])
    b=B['全員の1番目から多数決の3艇で3連複'];b[0]+=1;b[1]+=set(t)==set(trio);b[2]+=TRIO.get(k,0) if set(t)==set(trio) else 0
for k_,a in sorted(A.items()):print(f'  {k_}: {a[0]}R 1着率{a[1]/a[0]*100:.1f}%')
for k_,b in B.items():print(f'  {k_}: {b[0]}R 的中{b[1]/b[0]*100:.1f}% 回収{b[2]/b[0]:.0f}%')
print('== 一致の強さ別: 多数決3艇の3連複1点 / 多数決の1番目3連単')
C=collections.defaultdict(lambda:[0,0,0,0,0])
for k in keys:
    if k not in RES:continue
    t,p=RES[k];tt=t.split('-')
    tops=[S[s][k][0][0] for s in srcs if k in S[s]]
    if len(tops)<3:continue
    sets=collections.Counter('='.join(sorted(x.split('-'))) for x in tops)
    ts,tc=sets.most_common(1)[0]
    heads=collections.Counter(x[0] for x in tops);hh,hc=heads.most_common(1)[0]
    lvl=('3艇も全員一致' if tc==len(tops) else '3艇は過半数一致' if tc*2>len(tops) else '3艇ばらばら')+('・頭全員一致' if hc==len(tops) else '・頭割れ')
    tri3=collections.Counter(tops).most_common(1)[0][0]
    a=C[lvl];a[0]+=1
    if set(tt)==set(ts.split('=')):a[1]+=1;a[2]+=TRIO.get(k,0)
    if t==tri3:a[3]+=1;a[4]+=p
for k_,a in sorted(C.items(),key=lambda z:-z[1][0]):
    print(f'  {k_}: {a[0]}R | 3連複1点 的中{a[1]/a[0]*100:.1f}% 回収{a[2]/a[0]:.0f}% | 3連単1点 的中{a[3]/a[0]*100:.1f}% 回収{a[4]/a[0]:.0f}%')
