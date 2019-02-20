import itertools
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING

from negmas.apps.scml.simulators import FactorySimulator, SlowFactorySimulator
from negmas.common import NamedObject, MechanismState, MechanismInfo
from negmas.events import Event, EventSource, Notification
from negmas.helpers import ConfigReader, get_class
from negmas.negotiators import NegotiatorProxy
from negmas.outcomes import Issue, Outcome
from negmas.sao import AspirationNegotiator
from negmas.situated import Contract, Action, RenegotiationRequest
from negmas.utilities import UtilityFunctionProxy, UtilityValue, normalize
from .common import SCMLAgent, SCMLAgreement, Loan, CFP, Factory
from .consumers import ScheduleDrivenConsumer, ConsumptionProfile
from .schedulers import Scheduler, ScheduleInfo, GreedyScheduler
from .awi import SCMLAWI

if True:
    from typing import Dict, Iterable, Any, Callable, Collection, Type, List, Optional, Union


__all__ = [
    'FactoryManager', 'DoNothingFactoryManager', 'GreedyFactoryManager'
]


class FactoryManager(SCMLAgent, ConfigReader, ABC):
    """Base factory manager class that will be inherited by participant negmas in ANAC 2019"""

    def __init__(self, name=None, simulator_type: Union[str, Type[FactorySimulator]] = SlowFactorySimulator):
        super().__init__(name=name)
        self.transportation_delay = 0
        self.simulator: Optional[FactorySimulator] = None
        self.simulator_type: Type[FactorySimulator] = get_class(simulator_type, scope=globals())
        self.current_step = 0
        self.max_storage: int = 0

    def init(self):
        super().init()
        state: Factory = self.awi.state
        self.current_step = state.next_step
        self.max_storage = state.max_storage
        self.simulator = self.simulator_type(initial_wallet=state.wallet, initial_storage=state.storage
                                             , n_steps=self.awi.n_steps, n_products=len(self.products)
                                             , profiles=state.profiles, max_storage=self.max_storage)
        self.interesting_products = list(self.producing.keys())
        self.interesting_products += list(self.consuming.keys())

    def confirm_contract_execution(self, contract: Contract) -> bool:
        return True

    def on_breach_by_self(self, contract: Contract, victims: List[str]) -> Optional[RenegotiationRequest]:
        return None

    def on_breach_by_another(self, contract: Contract, partner: str) -> Optional[RenegotiationRequest]:
        return None

    def on_renegotiation_request(self, contract: Contract, cfp: "CFP", partner: str) -> bool:
        return False

    def confirm_loan(self, loan: Loan) -> bool:
        """called by the world manager to confirm a loan if needed by the buyer of a contract that is about to be
        breached"""
        return True

    def on_breach_meta_negotiation(self, contract: Contract
                                   , partner: str, issues: List[Issue]) -> Optional[NegotiatorProxy]:
        return None

    def on_event(self, event: Event, sender: EventSource):
        super().on_event(event=event, sender=sender)

    def step(self):
        self.simulator.fix_before(self.current_step)
        self.current_step += 1


class GreedyFactoryManager(FactoryManager):
    """The default factory manager that will be implemented by the committee of ANAC-SCML 2019"""

    def __init__(self, name=None, simulator_type: Union[str, Type[FactorySimulator]] = SlowFactorySimulator
                 , scheduler_type: Union[str, Type[Scheduler]] = GreedyScheduler
                 , scheduler_params: Optional[Dict[str, Any]] = None
                 , optimism: float = 0.0
                 , negotiator_type: Union[str, Type[NegotiatorProxy]] = 'negmas.sao.AspirationNegotiator'
                 , negotiator_params: Optional[Dict[str, Any]] = None
                 , n_retrials=5, use_consumer=True, reactive=True, sign_only_guaranteed_contracts=False
                 , riskiness=0.0, max_insurance_premium=None):
        super().__init__(name=name, simulator_type=simulator_type)
        self.negotiator_type = get_class(negotiator_type, scope=globals())
        self.negotiator_params = negotiator_params if negotiator_params is not None else {}
        self.optimism = optimism
        self.ufun_factory: Union[Type[NegotiatorUtility], Callable[[Any, Any], NegotiatorUtility]]
        if optimism < 1e-6:
            self.ufun_factory = PessimisticNegotiatorUtility
        elif optimism > 1 - 1e-6:
            self.ufun_factory = OptimisticNegotiatorUtility
        else:
            self.ufun_factory: NegotiatorUtility = lambda agent, annotation: \
                AveragingNegotiatorUtility(agent=agent, annotation=annotation, optimism=self.optimism)
        self.max_insurance_premium = max_insurance_premium
        self.n_retrials = n_retrials
        self.n_neg_trials: Dict[str, int] = defaultdict(int)
        self.consumer = None
        self.use_consumer = use_consumer
        self.reactive = reactive
        self.sign_only_guaranteed_contracts = sign_only_guaranteed_contracts
        self.contract_schedules: Dict[str, ScheduleInfo] = {}
        self.riskiness = riskiness
        self.negotiation_margin = int(round(n_retrials * riskiness))
        self.scheduler_type: Type[Scheduler] = get_class(scheduler_type, scope=globals())
        self.scheduler: Scheduler = None
        self.scheduler_params: Dict[str, Any] = scheduler_params if scheduler_params is not None else {}

    def total_utility(self, contracts: Collection[Contract] = ()) -> float:
        """Calculates the total utility for the agent of a collection of contracts"""
        if self.scheduler is None:
            raise ValueError('Cannot calculate total utility without a scheduler')
        bookmark = self.scheduler.bookmark()
        schedule = self.scheduler.schedule(contracts=contracts, assume_no_further_negotiations=False
                                           , ensure_storage_for=self.transportation_delay)
        self.scheduler.rollback(bookmark)
        if not schedule.valid:
            return -2000.0
        return schedule.final_balance

    def init(self):
        super().init()
        if self.use_consumer:
            # @todo add the parameters of the consumption profile as parameters of the greedy factory manager
            self.consumer: ScheduleDrivenConsumer = ScheduleDrivenConsumer(profiles=dict(zip(self.consuming.keys()
                                                                                             , (ConsumptionProfile(schedule=[_] * self.awi.n_steps)
                                                                    for _ in itertools.repeat(0))))
                                                                           , consumption_horizon=self.awi.n_steps, immediate_cfp_update=True
                                                                           , name=self.name)
            self.consumer.id = self.id
            self.consumer.awi = self.awi
            self.consumer.init()
        self.scheduler = self.scheduler_type(manager_id=self.id, awi=self.awi
                                             , max_insurance_premium=self.max_insurance_premium
                                             , **self.scheduler_params)
        self.scheduler.init(simulator=self.simulator, products=self.products, processes=self.processes
                            , producing=self.producing, profiles=self.compiled_profiles)

    def on_negotiation_request(self, cfp: "CFP", partner: str) -> Optional[NegotiatorProxy]:
        if self.use_consumer:
            return self.consumer.on_negotiation_request(cfp=cfp, partner=partner)
        else:
            neg = self.negotiator_type(name=self.name + '*' + partner, **self.negotiator_params)
            neg.utility_function = normalize(self.ufun_factory(self, self._create_annotation(cfp=cfp)),
                                             outcomes=cfp.outcomes, infeasible_cutoff=-1500)
            return neg

    def on_negotiation_success(self, contract: Contract, mechanism: MechanismInfo):
        super().on_negotiation_success(contract=contract, mechanism=mechanism)
        if self.use_consumer:
            self.consumer.on_negotiation_success(contract, mechanism)

    def on_negotiation_failure(self, partners: List[str], annotation: Dict[str, Any], mechanism: MechanismInfo
                               , state: MechanismState) -> None:
        super().on_negotiation_failure(partners=partners, annotation=annotation, mechanism=mechanism, state=state)
        if self.use_consumer:
            self.consumer.on_negotiation_failure(partners, annotation, mechanism, state)
        cfp = annotation['cfp']
        thiscfp = self.awi.bulletin_board.query(section='cfps', query=cfp.id, query_keys=True)
        if cfp.publisher != self.id and thiscfp is not None and len(thiscfp) > 0 \
            and self.n_neg_trials[cfp.id] < self.n_retrials:
            self.awi.logdebug(f'Renegotiating {self.n_neg_trials[cfp.id]} on {cfp}')
            self.on_new_cfp(cfp=annotation['cfp'])

    def _execute_schedule(self, schedule: ScheduleInfo, contract: Contract) -> None:
        if self.simulator is None:
            raise ValueError('No factory simulator is defined')
        awi: SCMLAWI = self.awi
        total = contract.agreement['unit_price'] * contract.agreement['quantity']
        product = contract.annotation['cfp'].product
        if contract.annotation['buyer'] == self.id:
            self.simulator.buy(product=product, quantity=contract.agreement['quantity']
                               , price=total, t=contract.agreement['time'])
            if self.max_insurance_premium is None or contract is None:
                return
            premium = awi.evaluate_insurance(contract=contract)
            if premium is  None or total <= 0:
                return
            relative_premium = premium / total
            if relative_premium <= self.max_insurance_premium:
                awi.buy_insurance(contract=contract)
                self.simulator.pay(premium, self.awi.current_step)
            return
        # I am a seller
        self.simulator.sell(product=product, quantity=contract.agreement['quantity']
                            , price=total, t=contract.agreement['time'])
        for job in schedule.jobs:
            awi.execute(action=Action(type=job.action, params={'profile': job.profile, 'time': job.time
                        , 'contract': contract, 'override': True}))
        for need in schedule.needs:
            if need.quantity_to_buy <= 0:
                continue
            product_id = need.product
            if self.use_consumer:
                self.consumer.profiles[product_id].schedule[need.step] += need.quantity_to_buy
                self.consumer.register_product_cfps(p=product_id, t=need.step
                                                    , profile=self.consumer.profiles[product_id])
                continue
            product = self.products[product_id]
            if product.catalog_price is None:
                price_range = (0.0, 100.0)
            else:
                price_range = (0.5, 1.5 * product.catalog_price)
            # @todo check this. This error is raised sometimes
            if need.step < awi.current_step:
                continue
                # raise ValueError(f'need {need} at {need.step} while running at step {awi.current_step}')
            time = need.step if self.max_storage is not None else (awi.current_step, need.step)
            cfp = CFP(is_buy=True, publisher=self.id, product=product_id
                      , time=time, unit_price=price_range
                      , quantity=(1, int(1.1 * need.quantity_to_buy)))
            awi.register_cfp(cfp)

    def sign_contract(self, contract: Contract):
        signature = super().sign_contract(contract)
        if signature is None:
            return None
        bookmark = self.scheduler.bookmark()
        schedule = self.scheduler.schedule(assume_no_further_negotiations=False, contracts=[contract]
                                           , ensure_storage_for=self.transportation_delay)
        self.scheduler.rollback(bookmark)
        if self.sign_only_guaranteed_contracts and (not schedule.valid or len(schedule.needs) > 1):
            return None
        self.contract_schedules[contract.id] = schedule
        return signature

    def on_contract_signed(self, contract: Contract):
        super().on_contract_signed(contract)
        if contract.annotation['buyer'] == self.id and self.use_consumer:
            self.consumer.on_contract_signed(contract)
        schedule = self.contract_schedules[contract.id]
        if schedule is not None and schedule.valid:
            self._execute_schedule(schedule=schedule, contract=contract)
        if contract.annotation['buyer'] != self.id or not self.use_consumer:
            for negotiation in self.running_negotiations.values():
                self.notify(negotiation.negotiator, Notification(type='ufun_modified', data=None))

    def _process_buy_cfp(self, cfp: 'CFP') -> None:
        if self.simulator is None or not self.can_expect_agreement(cfp=cfp, margin=self.negotiation_margin):
            return
        if not self.can_produce(cfp=cfp):
            return
        if self.negotiator_type == AspirationNegotiator:
            neg = self.negotiator_type(assume_normalized=True, name=self.name + '>' + cfp.publisher)
        else:
            neg = self.negotiator_type(name=self.name + '>' + cfp.publisher)
        neg.utility_function = normalize(self.ufun_factory(self, self._create_annotation(cfp=cfp)),
                                         outcomes=cfp.outcomes, infeasible_cutoff=-1500)
        self.request_negotiation(negotiator=neg, extra=None, partners=[cfp.publisher, self.id]
                                 , issues=cfp.issues, annotation=self._create_annotation(cfp=cfp)
                                 , mechanism_name='negmas.sao.SAOMechanism')

    def _process_sell_cfp(self, cfp: 'CFP'):
        if self.use_consumer:
            self.consumer.on_new_cfp(cfp=cfp)

    def on_new_cfp(self, cfp: 'CFP') -> None:
        if not self.reactive:
            return
        if cfp.satisfies(query={'is_buy': True, 'products': list(self.producing.keys())}):
            self._process_buy_cfp(cfp)
        if cfp.satisfies(query={'is_buy': False, 'products': list(self.consuming.keys())}):
            self._process_sell_cfp(cfp)

    def step(self):
        super().step()
        if self.use_consumer:
            self.consumer.step()
        if self.reactive:
            return
        # 0. remove all my CFPs
        # self.awi.bulletin_board.remove(section='cfps', query={'publisher': self})

        # respond to interesting CFPs
        # todo: should check time and sort products by interest etc
        cfps = self.awi.bulletin_board.query(section='cfps'
                                             , query={'products': self.producing.keys(), 'is_buy': True})
        if cfps is None:
            return
        for cfp in cfps.values():
            self._process_buy_cfp(cfp)

    def can_produce(self, cfp: CFP, assume_no_further_negotiations=False) -> bool:
        """Whether or not we can produce the required item in time"""
        if cfp.product not in self.producing.keys():
            return False
        agreement = SCMLAgreement(time=cfp.max_time, unit_price=cfp.max_unit_price, quantity=cfp.min_quantity)
        min_concluded_at = self.awi.current_step + 1 - int(self.immediate_negotiations)
        min_sign_at = min_concluded_at + self.awi.default_signing_delay
        if cfp.max_time < min_sign_at + 1:  # 1 is minimum time to produce the product
            return False
        bookmark = self.scheduler.bookmark()
        schedule = self.scheduler.schedule(contracts=[Contract(partners=[self.id, cfp.publisher]
                                                               , agreement=agreement
                                                               , annotation=self._create_annotation(cfp=cfp)
                                                               , issues=cfp.issues, signed_at=min_sign_at
                                                               , concluded_at=min_concluded_at)]
                                           , ensure_storage_for=self.transportation_delay
                                           , assume_no_further_negotiations=assume_no_further_negotiations)
        self.scheduler.rollback(bookmark)
        return schedule.valid and self.can_secure_needs(schedule=schedule, step=self.awi.current_step)

    def can_secure_needs(self, schedule: ScheduleInfo, step: int):
        """
        Finds if it is possible in principle to arrange these needs at the given time.

        Args:
            schedule:
            step:

        Returns:

        """
        needs = schedule.needs
        if len(needs) < 1:
            return True
        for need in needs:
            if need.quantity_to_buy > 0 and need.step < step + 1 - int(self.immediate_negotiations):  # @todo check this
                return False
        return True


class DoNothingFactoryManager(FactoryManager):
    """The default factory manager that will be implemented by the committee of ANAC-SCML 2019"""

    def on_new_cfp(self, cfp: 'CFP') -> None:
        pass

    def __init__(self, name=None):
        super().__init__(name=name)

    def on_negotiation_request(self, cfp: "CFP", partner: str) -> Optional[NegotiatorProxy]:
        return None

    def step(self):
        pass


TotalUtilityFun = Callable[[Collection[Contract]], float]


class NegotiatorUtility(UtilityFunctionProxy):
    """The utility function of a negotiator."""

    def __init__(self, agent: FactoryManager, annotation: Dict[str, Any], name: Optional[str] = None):
        if name is None:
            name = agent.name + '*' + '*'.join(_ for _ in annotation['partners'] if _ != agent.id)
        super().__init__(name=name)
        self.agent = agent
        self.annotation = annotation

    def _contracts(self, agreements: Iterable[SCMLAgreement]) -> Collection[Contract]:
        """Converts agreements/outcomes into contracts"""
        if self.info is None:
            raise ValueError('No annotation is stored (No mechanism info)')
        annotation = self.info.annotation
        return [Contract(partners=annotation['partners'], agreement=a, annotation=annotation, issues=self.info.issues)
                for a in agreements]

    def _contract(self, agreement: SCMLAgreement) -> Contract:
        """Converts an agreement/outcome into a contract"""
        annotation = self.annotation
        return Contract(partners=annotation['partners'], agreement=agreement
                        , annotation=annotation, issues=annotation['cfp'].issues)

    def _free_sale(self, agreement: SCMLAgreement) -> bool:
        return self.annotation['seller'] == self.agent and agreement['unit_price'] < 1e-6

    def __call__(self, outcome: Outcome) -> Optional[UtilityValue]:
        if isinstance(outcome, dict):
            return self.call(agreement=SCMLAgreement(**outcome))
        if isinstance(outcome, SCMLAgreement):
            return self.call(agreement=outcome)
        raise ValueError(f'Outcome: {outcome} cannot be converted to an SCMLAgreement')

    @abstractmethod
    def call(self, agreement: SCMLAgreement) -> Optional[UtilityValue]:
        """Called to evaluate a agreement"""

    def xml(self, issues: List[Issue]) -> str:
        return 'NegotiatorUtility has not xml representation'


class PessimisticNegotiatorUtility(NegotiatorUtility):
    """The utility function of a negotiator that assumes other negotiations currently open will fail."""

    def call(self, agreement: SCMLAgreement) -> Optional[UtilityValue]:
        """An offer will be a tuple of one value which in turn will be a list of contracts"""
        if self._free_sale(agreement):
            return -2000.0
        # contracts = self.agent.contracts
        # hypothetical = list(contracts)
        # hypothetical.append(self._contract(agreement))
        hypothetical = [self._contract(agreement)]
        base_util = self.agent.total_utility(contracts=())
        return self.agent.total_utility(hypothetical) - base_util


class OptimisticNegotiatorUtility(NegotiatorUtility):
    """The utility function of a negotiator that assumes other negotiations currently open will succeed."""

    def call(self, agreement: SCMLAgreement) -> Optional[UtilityValue]:
        if self._free_sale(agreement):
            return -2000.0
        # contracts = self.agent.contracts
        # hypothetical = list(contracts)
        # hypothetical.append(self._contract(agreement))
        hypothetical = [self._contract(agreement)]
        for negotiation in self.agent.running_negotiations.values():  # type: ignore
            negotiator = negotiation.negotiator
            current_offer = negotiator.my_last_proposal
            if current_offer is not None:
                hypothetical.append(self._contract(current_offer))
        base_util = self.agent.total_utility(contracts=())
        return self.agent.total_utility(list(hypothetical)) - base_util


class AveragingNegotiatorUtility(NegotiatorUtility):
    """A utility function that combines optimistic and pessimistic evaluators linearly using adjustable weight"""

    def __init__(self, agent: FactoryManager, annotation: Dict[str, Any], name: Optional[str] = None
                 , optimism: float = 0.5):
        NamedObject.__init__(self=self, name=name)
        self.optimism = optimism
        self.optimistic = OptimisticNegotiatorUtility(agent=agent, annotation=annotation)
        self.pessimistic = PessimisticNegotiatorUtility(agent=agent, annotation=annotation)

    def call(self, agreement: SCMLAgreement) -> Optional[UtilityValue]:
        if self._free_sale(agreement):
            return -2000.0
        opt, pess = self.optimistic(agreement), self.pessimistic(agreement)
        if opt is None or pess is None:
            return None
        return self.optimism * opt + (1 - self.optimism) * pess
