#!/usr/bin/env python

from __future__ import print_function

import math
import os
from collections import namedtuple

import numpy as np

from examples.continuous_tamp.constraint_solver import BLOCK_WIDTH, BLOCK_HEIGHT, GRASP
from examples.discrete_tamp.viewer import COLORS
from examples.continuous_tamp.constraint_solver import get_constraint_solver
from pddlstream.focused import solve_focused
from pddlstream.incremental import solve_incremental
from pddlstream.conversion import And, Equal
from pddlstream.fast_downward import TOTAL_COST
from pddlstream.stream import from_gen_fn, from_fn, from_test, Generator, StreamInfo
from pddlstream.utils import print_solution, user_input, read, INF
from viewer import ContinuousTMPViewer, GROUND

SCALE_COST = 1.

def scale_cost(cost):
    return int(math.ceil(SCALE_COST*cost))

##################################################

def interval_contains(i1, i2):
    """
    :param i1: The container interval
    :param i2: The possibly contained interval
    :return:
    """
    return (i1[0] <= i2[0]) and (i2[1] <= i1[1])

def interval_overlap(i1, i2):
    return (i2[0] <= i1[1]) and (i1[0] <= i2[1])

def get_block_interval(b, p):
    return p[0]*np.ones(2) + np.array([-BLOCK_WIDTH, +BLOCK_WIDTH]) / 2.

##################################################

def get_pose_generator(regions):
    class PoseGenerator(Generator):
        def __init__(self, *inputs):
            super(PoseGenerator, self).__init__()
            self.b, self.r = inputs
        def generate(self, outputs=None, streams=tuple()):
            # TODO: designate which streams can be handled
            placed = {}
            for stream in streams:
                name, args = stream[0], stream[1:]
                if name in ['collision-free', 'cfree']:
                    for i in range(0, len(args), 2):
                        b, p = args[i:i+2]
                        if self.b != b:
                            placed[b] = p
            #p = sample_region(self.b, regions[self.r])
            p = rejection_sample_region(self.b, regions[self.r], placed=placed)
            if p is None:
                return []
            return [(p,)]
    return PoseGenerator

def collision_test(b1, p1, b2, p2):
    return interval_overlap(get_block_interval(b1, p1), get_block_interval(b2, p2))

def distance_fn(q1, q2):
    ord = 1  # 1 | 2
    return scale_cost(np.linalg.norm(q2 - q1, ord=ord))

def inverse_kin_fn(b, p):
    return (p - GRASP,)

def get_region_test(regions):
    def test(b, p, r):
        return interval_contains(regions[r], get_block_interval(b, p))
    return test

def sample_region(b, region):
    x1, x2 = np.array(region, dtype=float) - get_block_interval(b, np.zeros(2))
    if x2 < x1:
        return None
    x = np.random.uniform(x1, x2)
    return np.array([x, 0])

def rejection_sample_region(b, region, placed={}, max_attempts=10):
    for _ in range(max_attempts):
        p = sample_region(b, region)
        if p is None:
            break
        if not any(collision_test(b, p, b2, p2) for b2, p2 in placed.items()):
            return p
    return None


def rejection_sample_placed(block_poses={}, block_regions={}, regions={}, max_attempts=10):
    assert(not set(block_poses.keys()) & set(block_regions.keys()))
    for _ in range(max_attempts):
        placed = block_poses.copy()
        remaining = block_regions.items()
        np.random.shuffle(remaining)
        for b, r in remaining:
            p = rejection_sample_region(b, regions[r], placed)
            if p is None:
                break
            placed[b] = p
        else:
            return placed
    return None

def get_pose_gen(regions):
    def gen_fn(b, r):
        while True:
            p = sample_region(b, regions[r])
            if p is None:
                break
            yield (p,)
    return gen_fn

def plan_motion(q1, q2):
    t = [q1, q2]
    #t = np.vstack([q1, q2])
    return (t,)

##################################################

def pddlstream_from_tamp(tamp_problem, constraint_solver=False):
    initial = tamp_problem.initial
    assert(initial.holding is None)

    directory = os.path.dirname(os.path.abspath(__file__))
    domain_pddl = read(os.path.join(directory, 'domain.pddl'))
    stream_pddl = read(os.path.join(directory, 'stream.pddl'))
    constant_map = {}

    init = [
        ('CanMove',),
        ('Conf', initial.conf),
        ('AtConf', initial.conf),
        ('HandEmpty',),
        Equal((TOTAL_COST,), 0)] + \
           [('Block', b) for b in initial.block_poses.keys()] + \
           [('Pose', b, p) for b, p in initial.block_poses.items()] + \
           [('Region', r) for r in tamp_problem.regions.keys()] + \
           [('AtPose', b, p) for b, p in initial.block_poses.items()] + \
           [('Placeable', b, GROUND) for b in initial.block_poses.keys()] + \
           [('Placeable', b, r) for b, r in tamp_problem.goal_regions.items()]

    goal_literals = [('In', b, r) for b, r in tamp_problem.goal_regions.items()]
    if tamp_problem.goal_conf is not None:
        goal_literals += [('AtConf', tamp_problem.goal_conf)]
    goal = And(*goal_literals)

    stream_map = {
        #'sample-pose': from_gen_fn(get_pose_gen(tamp_problem.regions)),
        'plan-motion': from_fn(plan_motion),
        'sample-pose': get_pose_generator(tamp_problem.regions),
        'test-region': from_test(get_region_test(tamp_problem.regions)),
        'inverse-kinematics':  from_fn(inverse_kin_fn),
        #'collision-free': from_test(lambda *args: not collision_test(*args)),
        'cfree': lambda *args: not collision_test(*args),
        'collision': collision_test,
        'distance': distance_fn,
    }
    if constraint_solver:
        stream_map['constraint-solver'] = get_constraint_solver(tamp_problem.regions)

    return domain_pddl, constant_map, stream_pddl, stream_map, init, goal

##################################################

TAMPState = namedtuple('TAMPState', ['conf', 'holding', 'block_poses'])
TAMPProblem = namedtuple('TAMPProblem', ['initial', 'regions', 'goal_conf', 'goal_regions'])

def get_tight_problem(n_blocks=1, n_goals=1):
    regions = {
        GROUND: (-15, 15),
        'red': (5, 10)
    }

    conf = np.array([0, 5])
    blocks = ['block{}'.format(i) for i in range(n_blocks)]
    #poses = [np.array([(BLOCK_WIDTH + 1)*x, 0]) for x in range(n_blocks)]
    poses = [np.array([-(BLOCK_WIDTH + 1) * x, 0]) for x in range(n_blocks)]
    #poses = [sample_pose(regions[GROUND]) for _ in range(n_blocks)]

    initial = TAMPState(conf, None, dict(zip(blocks, poses)))
    goal_regions = {block: 'red' for block in blocks[:n_goals]}

    return TAMPProblem(initial, regions, conf, goal_regions)

##################################################

def get_blocked_problem():
    goal = 'red'
    regions = {
        GROUND: (-15, 15),
        goal: (5, 10)
    }

    conf = np.array([0, 5])
    blocks = ['block{}'.format(i) for i in range(2)]
    #poses = [np.zeros(2), np.array([7.5, 0])]
    #block_poses = dict(zip(blocks, poses))

    block_regions = {
        blocks[0]: GROUND,
        blocks[1]: goal,
    }
    block_poses = rejection_sample_placed(block_regions=block_regions, regions=regions)

    initial = TAMPState(conf, None, block_poses)
    goal_regions = {blocks[0]: 'red'}

    return TAMPProblem(initial, regions, conf, goal_regions)

##################################################

def draw_state(viewer, state, colors):
    viewer.clear_state()
    viewer.draw_environment()
    viewer.draw_robot(*state.conf)
    for block, pose in state.block_poses.items():
        viewer.draw_block(pose[0], BLOCK_WIDTH, BLOCK_HEIGHT, color=colors[block])
    if state.holding is not None:
        viewer.draw_block(state.conf[0], BLOCK_WIDTH, BLOCK_HEIGHT, color=colors[state.holding])


def apply_action(state, action):
    conf, holding, block_poses = state
    # TODO: don't mutate block_poses?
    name = action[0]
    if name == 'move':
        _, _, conf = action[1:]
    elif name == 'pick':
        holding, _, _ = action[1:]
        del block_poses[holding]
    elif name == 'place':
        block, pose, _ = action[1:]
        holding = None
        block_poses[block] = pose
    else:
        raise ValueError(name)
    return TAMPState(conf, holding, block_poses)

##################################################

def main(focused=True, deterministic=False):
    np.set_printoptions(precision=2)
    if deterministic:
        np.random.seed(0)

    problem_fn = get_tight_problem
    tamp_problem = problem_fn()
    print(tamp_problem)

    stream_info = {
        'test-region': StreamInfo(eager=True, p_success=0), # bound_fn is None
        #'cfree': StreamInfo(eager=True),
    }

    pddlstream_problem = pddlstream_from_tamp(tamp_problem)
    if focused:
        solution = solve_focused(pddlstream_problem, stream_info=stream_info,
                                 max_time=10, max_cost=INF, debug=False,
                                 commit=True, effort_weight=None, unit_costs=False, visualize=False)
    else:
        solution = solve_incremental(pddlstream_problem, layers=1, unit_costs=False)
    print_solution(solution)
    plan, cost, evaluations = solution
    if plan is None:
        return

    colors = dict(zip(sorted(tamp_problem.initial.block_poses.keys()), COLORS))
    viewer = ContinuousTMPViewer(tamp_problem.regions, title='Continuous TAMP')
    state = tamp_problem.initial
    print(state)
    draw_state(viewer, state, colors)
    for action in plan:
        user_input('Continue?')
        state = apply_action(state, action)
        print(state)
        draw_state(viewer, state, colors)
    user_input('Finish?')


if __name__ == '__main__':
    main()
