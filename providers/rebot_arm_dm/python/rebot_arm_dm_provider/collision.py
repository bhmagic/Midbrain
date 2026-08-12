"""Hardware-Development-UI-only conservative current-pose collision checks.

This module is not imported by the Basic Controller. It exists solely in the standalone
hardware development application and must not be treated as an operational planner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import math
import numpy as np

from .kinematics import RebotKinematics


def _segment_distance(p1:np.ndarray,q1:np.ndarray,p2:np.ndarray,q2:np.ndarray) -> float:
    # Closest distance between two finite 3D line segments.
    d1=q1-p1; d2=q2-p2; r=p1-p2; a=float(d1@d1); e=float(d2@d2); f=float(d2@r); eps=1e-12
    if a<=eps and e<=eps: return float(np.linalg.norm(p1-p2))
    if a<=eps: s=0.0; t=float(np.clip(f/e,0.0,1.0))
    else:
        c=float(d1@r)
        if e<=eps: t=0.0; s=float(np.clip(-c/a,0.0,1.0))
        else:
            b=float(d1@d2); denom=a*e-b*b
            s=0.0 if abs(denom)<=eps else float(np.clip((b*f-c*e)/denom,0.0,1.0))
            t=(b*s+f)/e
            if t<0.0: t=0.0; s=float(np.clip(-c/a,0.0,1.0))
            elif t>1.0: t=1.0; s=float(np.clip((b-c)/a,0.0,1.0))
    return float(np.linalg.norm((p1+d1*s)-(p2+d2*t)))


@dataclass
class CollisionResult:
    safe: bool
    minimum_clearance_m: float
    minimum_safety_margin_m: float
    reason: str | None
    pair: list[int] | None = None

    def to_dict(self): return self.__dict__.copy()


class CalibrationCollisionGuard:
    def __init__(self,kinematics:RebotKinematics,configuration:dict[str,Any]):
        self.kinematics=kinematics; self.configuration=configuration
        self.radii=np.asarray(configuration['capsule_radii_m'],dtype=float)
        self.allowed={tuple(sorted(map(int,p))) for p in configuration.get('allowed_adjacent_pairs',[])+configuration.get('extra_allowed_pairs',[])}
        self.overrides={tuple(int(v) for v in key.split(',')):float(value) for key,value in configuration.get('pair_clearance_overrides_m',{}).items()}

    @classmethod
    def load(cls,kinematics:RebotKinematics,path:str):
        with open(path,'r',encoding='utf-8') as stream: return cls(kinematics,json.load(stream))

    def check(self,positions_rad:Any,table_height_m:float|None=None,table_clearance_m:float|None=None) -> CollisionResult:
        points=np.asarray(self.kinematics.points(positions_rad),dtype=float)
        if len(points)!=8: raise RuntimeError('expected eight kinematic points')
        table=self.configuration['table']; height=float(table['height_m'] if table_height_m is None else table_height_m)
        extra=float(table['clearance_m'] if table_clearance_m is None else table_clearance_m)
        minimum_margin=math.inf; corresponding_clearance=math.inf; reason=None; pair=None
        skipped={int(v) for v in table.get('skip_capsule_indices',[0])}
        for i in range(7):
            if i in skipped: continue
            clearance=min(points[i,2],points[i+1,2])-self.radii[i]-height-extra
            margin=clearance
            if margin<minimum_margin: minimum_margin=margin; corresponding_clearance=clearance; reason=f'link {i+1} approaches the desktop'; pair=[i,-1]
        required=float(self.configuration['sampling'].get('minimum_cartesian_clearance_m',0.005))
        for i in range(7):
            for j in range(i+1,7):
                if (i,j) in self.allowed: continue
                clearance=_segment_distance(points[i],points[i+1],points[j],points[j+1])-self.radii[i]-self.radii[j]
                threshold=float(self.overrides.get((i,j),required))
                margin=clearance-threshold
                if margin<minimum_margin: minimum_margin=margin; corresponding_clearance=clearance; reason=f'link {i+1} approaches link {j+1}'; pair=[i,j]
        safe=bool(minimum_margin>=0.0)
        return CollisionResult(safe,float(corresponding_clearance),float(minimum_margin),None if safe else reason,pair)
