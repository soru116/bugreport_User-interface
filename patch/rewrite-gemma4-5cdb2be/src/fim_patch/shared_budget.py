"""A conservative per-ticket infill ledger shared by every hole and retry."""
VERSION='shared-infill-budget-v1'


class BudgetExceeded(ValueError):
    pass


class SharedBudgetBackend:
    def __init__(self,backend,limit):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=3:
            raise ValueError('shared generation limit must be 1..3')
        self.backend,self.limit,self.events=backend,limit,[]
        self.generate_dispatches=0
        self._metadata=None
    def __getattr__(self,name):return getattr(self.backend,name)
    @property
    def remaining(self):return self.limit-len(self.events)
    def describe(self):
        if self._metadata is None:self._metadata=self.backend.describe()
        return self._metadata # metadata probes are not repair attempts
    def infill(self,prefix,suffix,**kwargs):
        if not self.remaining:raise BudgetExceeded('shared_generation_budget_exhausted')
        event=dict(attempt=len(self.events)+1,seed=kwargs.get('seed'),output_limit=kwargs.get('max_new_tokens'),status='started')
        self.events.append(event)
        original=getattr(self.backend,'post',None)
        if original:
            def tracked(endpoint,payload):
                if endpoint=='generate':self.generate_dispatches+=1
                return original(endpoint,payload)
            self.backend.post=tracked
        try:
            reply=self.backend.infill(prefix,suffix,**kwargs)
            event['status']='returned'
            return reply
        except Exception as exc:
            event.update(status='error',error_type=type(exc).__name__)
            raise
        finally:
            if original:self.backend.post=original
    def report(self):
        return dict(version=VERSION,limit=self.limit,infill_attempts=len(self.events),
                    backend_generate_dispatches=self.generate_dispatches,remaining=self.remaining,events=list(self.events),
                    note='Conservative attempt cap; dispatches may be saved-response replay. Metadata probes excluded.')
