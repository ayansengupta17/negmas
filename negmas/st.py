"""Implements single text negotiation mechanisms"""
import itertools
import math
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional, Tuple, Union

from negmas import MechanismRoundResult, Outcome, MechanismState
from negmas.mechanisms import Mechanism

__all__ = [
    "VetoSTMechanism",
]


@dataclass
class STState(MechanismState):
    """Defines extra values to keep in the mechanism state. This is accessible to all negotiators"""
    current_offer: Optional["Outcome"] = None
    new_offer: Optional["Outcome"] = None


class VetoSTMechanism(Mechanism):
    """Base class for all single text mechanisms

    Args:
        *args: positional arguments to be passed to the base Mechanism
        **kwargs: keyword arguments to be passed to the base Mechanism
        initial_outcome: initial outcome. If None, it will be selected by `next_outcome` which by default will choose it
                         randomly.
        initial_responses: Initial set of responses.

    Remarks:

        - initial_responses is only of value when the number of negotiators that will join the negotiation is less then
          or equal to its length. By default it is not used for anything. Nevertheless, it is here because
          `next_outcome` may decide to use it with the `initial_outcome`


    """

    def __init__(self, *args, epsilon: float = 1e-6, initial_outcome=None, initial_responses: Tuple[bool] = tuple()
                 , **kwargs):
        kwargs["state_factory"] = STState
        super().__init__(*args, **kwargs)

        self.add_requirements({"compare-binary": True}) # assert that all agents must have compare-binary capability
        self.current_offer = initial_outcome
        """The current offer"""
        self.initial_outcome = deepcopy(initial_outcome)
        """The initial offer"""
        self.last_responses = list(initial_responses)
        """The responses of all negotiators for the last offer"""
        self.initial_responses = deepcopy(self.last_responses)
        """The initial set of responses. See the remarks of this class to understand its role."""
        self.epsilon = epsilon
        self.new_offer = initial_outcome
        """The new offer generated in this step"""

    def extra_state(self):
        return STState(
            current_offer=deepcopy(self.current_offer),
            new_offer=deepcopy(self.new_offer),
        )

    def next_outcome(self, outcome: Optional[Outcome]) -> Optional[Outcome]:
        """Generate the next outcome given some outcome.

        Args:
             outcome: The current outcome

        Returns:
            a new outcome or None to end the mechanism run

        """
        return self.random_outcomes(1)[0]

    def round(self) -> MechanismRoundResult:
        """Single round of the protocol"""
        new_offer = self.next_outcome(self.current_offer)
        responses = []

        for neg in self.negotiators:
            strt = time.perf_counter()
            responses.append(neg.is_better(new_offer, self.current_offer, epsilon=self.epsilon) is not False)
            if time.perf_counter() - strt > self.ami.step_time_limit:
                return MechanismRoundResult(
                    broken=False, timedout=True, agreement=None
                )

        self.last_responses = responses
        self.new_offer = new_offer
        if all(responses):
            self.current_offer = new_offer


        return MechanismRoundResult(broken=False, timedout=False, agreement=None)

    def on_negotiation_end(self) -> None:
        """Used to pass the final offer for agreement between all negotiators"""
        if self.current_offer is not None and all(neg.is_acceptable_as_agreement(self.current_offer) for neg in self.negotiators):
            self._agreement = self.current_offer

        super().on_negotiation_end()

    def plot(
        self,
        visible_negotiators: Union[Tuple[int, int], Tuple[str, str]] = (0, 1),
        show_all_offers=False,
    ):
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        import pandas as pd

        if len(self.negotiators) < 2:
            print("Cannot visualize negotiations with more less than 2 negotiators")
            return
        if len(visible_negotiators) > 2:
            print("Cannot visualize more than 2 agents")
            return
        if isinstance(visible_negotiators[0], str):
            tmp = []
            for _ in visible_negotiators:
                for n in self.negotiators:
                    if n.id == _:
                        tmp.append(n)
        else:
            visible_negotiators = [
                self.negotiators[visible_negotiators[0]],
                self.negotiators[visible_negotiators[1]],
            ]
        indx = dict(zip([_.id for _ in self.negotiators], range(len(self.negotiators))))
        history = []
        for state in self.history:
            offer = state.new_offer if show_all_offers else state.current_offer
            history.append(
                {
                    "current_offer": offer,
                    "relative_time": state.relative_time,
                    "step": state.step,
                    "u0": visible_negotiators[0].utility_function(offer),
                    "u1": visible_negotiators[1].utility_function(offer),
                }
            )
        history = pd.DataFrame(data=history)
        has_history = len(history) > 0
        has_front = 1
        n_negotiators = len(self.negotiators)
        n_agents = len(visible_negotiators)
        ufuns = self._get_ufuns()
        outcomes = self.outcomes
        utils = [tuple(f(o) for f in ufuns) for o in outcomes]
        agent_names = [a.name for a in visible_negotiators]
        frontier, frontier_outcome = self.pareto_frontier(sort_by_welfare=True)
        frontier_indices = [
            i
            for i, _ in enumerate(frontier)
            if _[0] is not None
            and _[0] > float("-inf")
            and _[1] is not None
            and _[1] > float("-inf")
        ]
        frontier = [frontier[i] for i in frontier_indices]
        frontier_outcome = [frontier_outcome[i] for i in frontier_indices]
        frontier_outcome_indices = [outcomes.index(_) for _ in frontier_outcome]

        fig_util = plt.figure()
        gs_util = gridspec.GridSpec(n_agents, has_front + 1)
        axs_util = []

        for a in range(n_agents):
            if a == 0:
                axs_util.append(fig_util.add_subplot(gs_util[a, has_front]))
            else:
                axs_util.append(
                    fig_util.add_subplot(gs_util[a, has_front], sharex=axs_util[0])
                )
            axs_util[-1].set_ylabel(agent_names[a])
        for a, au in enumerate(axs_util):
            if au is None:
                break
            if has_history:
                h = history.loc[:,
                    ["step", "current_offer", "u0", "u1"],
                ]
                h["utility"] = h[f"u{a}"]
                au.plot(h.step, h.utility)
                au.set_ylim(0.0, 1.0)

        if has_front:
            axu = fig_util.add_subplot(gs_util[:, 0])
            axu.scatter(
                [_[0] for _ in utils],
                [_[1] for _ in utils],
                label="outcomes",
                color="gray",
                marker="s",
                s=20,
            )
            f1, f2 = [_[0] for _ in frontier], [_[1] for _ in frontier]
            axu.scatter(f1, f2, label="frontier", color="red", marker="x")
            # axu.legend()
            axu.set_xlabel(agent_names[0] + " utility")
            axu.set_ylabel(agent_names[1] + " utility")
            if self.agreement is not None:
                pareto_distance = 1e9
                cu = (ufuns[0](self.agreement), ufuns[1](self.agreement))
                for pu in frontier:
                    dist = math.sqrt((pu[0] - cu[0]) ** 2 + (pu[1] - cu[1]) ** 2)
                    if dist < pareto_distance:
                        pareto_distance = dist
                axu.text(
                    0.05,
                    0.05,
                    f"Pareto-distance={pareto_distance:5.2}",
                    verticalalignment="top",
                    transform=axu.transAxes,
                )

            if has_history:
                h = history.loc[:,
                    ["step", "current_offer", "u0", "u1"],
                ]
                axu.scatter(h.u0, h.u1, color="green", label=f"Mediator's Offer")
                axu.scatter([frontier[0][0]], [frontier[0][1]], color="blue", label=f"Max Welfare")
                # axu.annotate(
                #     "Max. Welfare",
                #     xy=frontier[0],  # theta, radius
                #     xytext=(
                #         frontier[0][0] + 0.1,
                #         frontier[0][1] + 0.02,
                #     ),  # fraction, fraction
                #     arrowprops=dict(facecolor="black", shrink=0.05),
                #     horizontalalignment="left",
                #     verticalalignment="bottom",
                # )
            if self.state.agreement is not None:
                axu.scatter(
                    [ufuns[0](self.state.agreement)],
                    [ufuns[1](self.state.agreement)],
                    color="black",
                    marker="*",
                    s=120,
                    label="Agreement",
                )

        fig_util.show()
