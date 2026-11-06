"""Small, past-only probability corrections and Dixon-Coles goal correlation."""
from __future__ import annotations

from datetime import date
import math

import numpy as np
from scipy.optimize import minimize,minimize_scalar

from research.models import LearnedPoisson


def softmax(values):
    values=np.asarray(values,dtype=float);result=np.exp(values-values.max(axis=-1,keepdims=True))
    return result/result.sum(axis=-1,keepdims=True)


def fit_calibration(examples,cutoff,settings):
    prior=[r for r in examples if 0<(cutoff-date.fromisoformat(r['date'])).days<=settings['windowDays']]
    if len(prior)<300:raise ValueError('Not enough past calibration examples')
    source=settings['source'];logs=np.log(np.maximum(1e-12,np.array([r[source] for r in prior])))
    labels=np.array([r['actual'] for r in prior]);target=np.eye(3)[labels]
    weights=np.exp(-math.log(2)*np.array([(cutoff-date.fromisoformat(r['date'])).days for r in prior])/settings['halfLife']);weights/=weights.sum()
    mode=settings['mode'];ridge=settings['ridge']
    identity=np.eye(3).ravel()
    initial=np.array([1.]) if mode=='temperature' else np.array([1.,0.,0.]) if mode=='temperature_bias' else np.r_[identity,np.zeros(3)]
    bounds=[(.6,1.8)] if mode=='temperature' else [(.6,1.8),(-.5,.5),(-.5,.5)] if mode=='temperature_bias' else [(-1,3)]*9+[(-.5,.5)]*3
    def objective(theta):
        if mode=='matrix':logits=logs@theta[:9].reshape(3,3).T+theta[9:]
        else:logits=theta[0]*logs+(np.array([theta[1],0,theta[2]]) if mode=='temperature_bias' else 0)
        p=softmax(logits);residual=weights[:,None]*(p-target)
        ce=-np.sum(weights*np.log(np.maximum(1e-12,p[np.arange(len(prior)),labels])))
        if mode=='matrix':gradient=np.r_[(residual.T@logs).ravel(),residual.sum(axis=0)]
        else:
            gradient=np.array([np.sum(residual*logs)])
            if mode=='temperature_bias':gradient=np.r_[gradient,residual[:,0].sum(),residual[:,2].sum()]
        delta=theta-initial
        return ce+ridge*np.sum(delta**2)/2,gradient+ridge*delta
    result=minimize(objective,initial,jac=True,method='L-BFGS-B',bounds=bounds,options={'maxiter':150,'ftol':1e-11,'gtol':1e-7})
    if not result.success or not np.all(np.isfinite(result.x)):raise RuntimeError('Calibration did not converge')
    return {'mode':mode,'parameters':result.x.tolist(),'trainingRows':len(prior),'latestTrainingMatch':prior[-1]['date'],'cutoffExclusive':cutoff.isoformat()}


def calibrated(probabilities,model):
    logs=np.log(np.maximum(1e-12,np.asarray(probabilities)));theta=np.array(model['parameters'])
    if model['mode']=='matrix':logits=logs@theta[:9].reshape(3,3).T+theta[9:]
    else:logits=theta[0]*logs+(np.array([theta[1],0,theta[2]]) if model['mode']=='temperature_bias' else 0)
    return softmax(logits).tolist()


def calibration_predictions(examples,rows,settings):
    result=[];last=None;model=None
    for r in rows:
        cutoff=date.fromisoformat(r['cutoff_date'])
        if cutoff!=last:model=fit_calibration(examples,cutoff,settings);last=cutoff
        result.append(calibrated(r[settings['source']],model))
    return result


class DixonColes(LearnedPoisson):
    def fit(self,matches,day):
        super().fit(matches,day)
        prior=[m for m in matches if 0<(day-m.day).days<=self.window_days]
        index={name:i for i,name in enumerate(self.teams)};n=len(index)
        hi=np.array([index[m.home] for m in prior]);ai=np.array([index[m.away] for m in prior]);t=self.theta
        h=np.clip(np.exp(t[0]+t[1]+t[2+hi]-t[2+n+ai]),.05,5);a=np.clip(np.exp(t[0]+t[2+ai]-t[2+n+hi]),.05,5)
        yh=np.array([m.home_goals for m in prior]);ya=np.array([m.away_goals for m in prior]);coeff=np.zeros(len(prior))
        coeff[(yh==0)&(ya==0)]=(-h*a)[(yh==0)&(ya==0)]
        coeff[(yh==0)&(ya==1)]=h[(yh==0)&(ya==1)]
        coeff[(yh==1)&(ya==0)]=a[(yh==1)&(ya==0)]
        coeff[(yh==1)&(ya==1)]=-1
        lower=max(-.2,max((-1/value+1e-8 for value in coeff if value>0),default=-.2))
        upper=min(.2,min((-1/value-1e-8 for value in coeff if value<0),default=.2))
        weights=np.exp(-math.log(2)*np.array([(day-m.day).days for m in prior])/self.half_life)
        objective=lambda rho:-np.sum(weights*np.log(1+coeff*rho))/weights.sum()+.05*rho*rho
        result=minimize_scalar(objective,bounds=(lower,upper),method='bounded')
        if not result.success:raise RuntimeError('Dixon-Coles correction did not converge')
        self.rho=float(result.x);return self

    def predict(self,home,away,day=None):
        original=super().predict(home,away,day);h=original['home_goals'];a=original['away_goals']
        rho=max(max(-1/h,-1/a)+1e-9,min(min(1,1/(h*a))-1e-9,self.rho))
        hp=np.array([math.exp(-h)*h**i/math.factorial(i) for i in range(21)]);ap=np.array([math.exp(-a)*a**i/math.factorial(i) for i in range(21)])
        matrix=np.outer(hp,ap);matrix[0,0]*=1-h*a*rho;matrix[0,1]*=1+h*rho;matrix[1,0]*=1+a*rho;matrix[1,1]*=1-rho
        values=np.array([np.tril(matrix,-1).sum(),np.trace(matrix),np.triu(matrix,1).sum()]);original['probabilities']=values/values.sum()
        return original

    def export(self):
        result=super().export();result['dixon_coles_rho']=self.rho;result['model_version']='dixon-coles-0.1';return result
