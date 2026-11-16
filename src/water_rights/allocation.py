from collections import defaultdict

def allocate(basins,claims):
    by_basin=defaultdict(list)
    for c in claims:by_basin[c['basin']].append(c)
    result=[]
    for basin,cap in sorted(basins.items()):
        left=cap;rows_out=[]
        groups=defaultdict(list)
        for c in by_basin[basin]:groups[c['priority']].append(c)
        for priority,group in sorted(groups.items()):
            total=sum(c['requested'] for c in group)
            if total<=left:shares={c['id']:c['requested'] for c in group}
            elif total:
                shares={c['id']:left*c['requested']//total for c in group};remaining=left-sum(shares.values())
                order=sorted(group,key=lambda c:(-(left*c['requested']%total),c['id']))
                for c in order[:remaining]:shares[c['id']]+=1
            else:shares={c['id']:0 for c in group}
            for c in sorted(group,key=lambda c:c['id']):rows_out.append({'id':c['id'],'priority':priority,'requested':c['requested'],'allocated':shares[c['id']],'unmet':c['requested']-shares[c['id']]})
            left-=sum(shares.values())
        result.append({'basin':basin,'cap':cap,'allocated':cap-left,'remaining':left,'claims':rows_out})
    return {'basins':result}
