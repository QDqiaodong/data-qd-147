from .common import require,ident,number,rows
from .allocation import allocate

def analyze(data):
    require(data,'basins claims');basins={}
    for b in rows(data['basins'],100):
        require(b,'id cap');bid=ident(b['id']);cap=number(b['cap'],0,10**12)
        if bid in basins:raise ValueError('duplicate basin')
        basins[bid]=cap
    claims=[];seen=set()
    for c in rows(data['claims'],20000):
        require(c,'id basin priority requested');cid=ident(c['id']);basin=ident(c['basin']);priority=number(c['priority'],0,10**6);requested=number(c['requested'],0,10**12)
        if basin not in basins:raise ValueError('unknown basin')
        if cid in seen:raise ValueError('duplicate claim')
        seen.add(cid)
        claims.append({'id':cid,'basin':basin,'priority':priority,'requested':requested})
    return allocate(basins,claims)
