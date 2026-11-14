from fractions import Fraction
import math

def wilson(successes,n):
    if not n:return None
    z=1.959963984540054;p=successes/n;den=1+z*z/n
    center=(p+z*z/(2*n))/den
    radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return {'lower':center-radius,'upper':center+radius,'method':'Wilson score 95%, normal quantile approximation'}

def compare(c,t):
    nc,xc=c;nt,xt=t;pc=Fraction(xc,nc);pt=Fraction(xt,nt);difference=pt-pc
    se=math.sqrt(float(pc*(1-pc)/nc+pt*(1-pt)/nt))
    return {'risk_difference':str(difference),'relative_lift':str(pt/pc-1) if pc else None,
            'risk_ratio':str(pt/pc) if pc else None,'standard_error_difference':se,
            'difference_interval_95':{'lower':float(difference)-1.959963984540054*se,
                                      'upper':float(difference)+1.959963984540054*se,
                                      'method':'Unpooled normal approximation; not clipped to parameter bounds'}}
