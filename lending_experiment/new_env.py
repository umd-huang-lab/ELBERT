'''
New Lending Env with the following modification from Eric's code
  1. When reject, it is still possible for the state to change as if the loan is approved. (for adding stochasticity)
  2. The transition dynamics allows "jumping" from non-contiguent states (for more 'long-term' effect)
'''

from lending_experiment.environments.lending import * 


############################ new env Parameters ############################
from lending_experiment.config import ALTER_RATE, TRANSITION_DYNAMICS  

# the following will eventually writes to config.py

############################ new env Starts ############################
# the following will eventually writes to lending.py
class _GeneralCreditShift(core.StateUpdater):
  '''
  Chenghao's code, with the following modification from the original CreditShift function
  1. When reject, it is still possible for the state to change as if the loan is approved. (for adding stochasticity)
  2. The transition dynamics allows "jumping" from non-contiguent states (for more 'long-term' effect)
  '''
  """Updates the cluster probabilities based on the repayment."""

  def update(self, state, action):
    """Updates the cluster probabilities based on the repayment.
    Successful repayment raises one's credit score and default lowers one's
    credit score. This is expressed by moving a small amount of probability mass
    (representing an individual) from one credit-score cluster to an adjacent
    one.
    This change in credit only happens if the applicant is accepted. Rejected
    applicants experience no change in their score.
    state.params is mutated in place; nothing is returned.
    Args:
      state: A core.State object.
      action: a `LoanDecision`.
    """

    # if action == LoanDecision.REJECT:
    #   return

    params = state.params
    group_id = state.group_id

    # Group should always be a one-hot encoding of group_id. This assert tests
    # that these two values have not somehow gotten out of sync.
    assert state.group_id == np.argmax(
        state.group), 'Group id %s. group %s' % (state.group_id,
                                                 np.argmax(state.group))

    # Cast to list so we can mutate it.
    cluster_probs = list(
        params.applicant_distribution.components[group_id].weights)

    rng = np.random.RandomState()
    for _ in range(10):
      group = params.applicant_distribution.components[group_id].sample(
          rng).group
      assert np.array_equal(group, state.group), (
          'Sampling from the component that is indexed here does not give '
          'members of the group that is intended to be affected. Something is '
          'quite wrong. Check that your group ids are in order in the credit '
          'cluster spec. sampled group_id %s vs state.group %s. '
          'Component[%d]: %s' %
          (group, state.group, group_id,
           params.applicant_distribution.components[group_id]))

    # Assert argmax gives the right index.
    for idx, component in enumerate(
        params.applicant_distribution.components[group_id].components):
      credit_score = component.sample(rng).features
      assert np.argmax(credit_score) == idx, '%s vs %s' % (credit_score, idx)

    # This applicant has their credit score lowered or raised.
    cluster_id = np.argmax(state.applicant_features)
    #### new_cluster = (cluster_id - 1 if state.will_default else cluster_id + 1)
    dis = np.array(TRANSITION_DYNAMICS[state.will_default][cluster_id])
    if action == LoanDecision.REJECT:
      dis = ALTER_RATE[group_id] * dis
      dis[cluster_id] = dis[cluster_id] + (1 - ALTER_RATE[group_id])
    new_cluster = np.random.choice(np.arange(len(cluster_probs)), p=dis)

    # Prevents falling off the edges of the cluster array.
    new_cluster = min(new_cluster, len(cluster_probs) - 1)
    new_cluster = max(new_cluster, 0)

    # Prevents moving more probability mass than this bucket has.
    assert cluster_probs[cluster_id] > 0, (
        'This cluster was sampled but has no mass. %d. Full distribution %s' %
        (cluster_id, cluster_probs))

    mass_to_shift = min(params.cluster_shift_increment,
                        cluster_probs[cluster_id])

    # Mutates params.cluster_probs[group_id].
    cluster_probs[cluster_id] -= mass_to_shift
    cluster_probs[new_cluster] += mass_to_shift
    logging.debug('Group %d: Moving mass %f from %d to %d', group_id,
                  mass_to_shift, cluster_id, new_cluster)

    assert np.abs(np.sum(cluster_probs) -
                  1) < 1e-6, 'Cluster probs must sum to 1.'
    assert all([prob >= 0 for prob in cluster_probs
               ]), 'Cluster probs must be non-negative'

    state.params.applicant_distribution.components[
        group_id].weights = cluster_probs


class GeneralDelayedImpactEnv(BaseLendingEnv):
  """
  Chenghao's code: using _GeneralCreditShift for state transition
  """
  default_param_builder = lending_params.DelayedImpactParams
  _parameter_updater = _GeneralCreditShift()

  def __init__(self, params=None):
    super(GeneralDelayedImpactEnv, self).__init__(params)
    self.observable_state_vars['applicant_features'] = multinomial.Multinomial(
        self.initial_params.applicant_distribution.dim, 1)
    self.observation_space = spaces.Dict(self.observable_state_vars)
############################ new env Ends ############################