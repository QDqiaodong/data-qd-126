from fractions import Fraction
from .common import require,ident,number,rows
from .statistics import wilson,compare

def analyze(data):
    require(data,'control treatment expected_treatment_share assignments outcomes');control=ident(data['control']);treatment=ident(data['treatment'])
    if control==treatment:raise ValueError('control and treatment must differ')
    share=data['expected_treatment_share'];require(share,'numerator denominator')
    numerator=number(share['numerator'],1);denominator=number(share['denominator'],2)
    if numerator>=denominator:raise ValueError('expected share must be strictly between zero and one')
    expected=Fraction(numerator,denominator);assigned={};outcomes={};counts={control:[0,0],treatment:[0,0]}
    for a in rows(data['assignments'],20000):
        require(a,'user variant');user=ident(a['user']);variant=ident(a['variant'])
        if user in assigned or variant not in counts:raise ValueError('duplicate assignment or unknown variant')
        assigned[user]=variant;counts[variant][0]+=1
    for o in rows(data['outcomes'],20000):
        require(o,'user converted');user=ident(o['user']);converted=number(o['converted'],0,1)
        if user in outcomes or user not in assigned:raise ValueError('duplicate or unassigned outcome')
        outcomes[user]=converted;counts[assigned[user]][1]+=converted
    if set(assigned)!=set(outcomes):raise ValueError('every assigned unit needs an explicit observed 0/1 outcome')
    if any(n==0 for n,x in counts.values()):raise ValueError('both arms require observed assignments')
    total=len(assigned);expected_counts={control:total*(1-expected),treatment:total*expected}
    statistic=sum(((Fraction(counts[v][0])-e)**2/e for v,e in expected_counts.items()),Fraction())
    return {'arms':{v:{'assigned':n,'converted':x,'rate':str(Fraction(x,n)),'rate_interval_95':wilson(x,n)} for v,(n,x) in sorted(counts.items())},
            'comparison':compare(counts[control],counts[treatment]),
            'allocation_diagnostic':{'pearson_chi_square':str(statistic),'expected_counts':{v:str(e) for v,e in expected_counts.items()},
                                     'p_value':None,'interpretation':'Count diagnostic only; no automatic pass/fail or experiment stopping decision.'}}
