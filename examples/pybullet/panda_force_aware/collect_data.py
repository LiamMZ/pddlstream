#!/usr/bin/env python

from __future__ import print_function

from examples.pybullet.panda_force_aware.streams import get_cfree_approach_pose_test, get_cfree_pose_pose_test, get_cfree_traj_pose_test, \
    get_cfree_traj_grasp_pose_test, distance_fn

from examples.pybullet.utils.pybullet_tools.panda_primitives_v2 import Pose, Conf, get_ik_ir_gen, \
    get_stable_gen, get_grasp_gen, control_commands, get_torque_limits_not_exceded_test, \
    get_stable_gen_dumb, get_torque_limits_mock_test, get_ik_ir_gen_no_reconfig, hack_table_place,\
    get_ik_ir_gen_force_aware, get_torques_exceded_global, get_mass, METHOD, reset_torques_exceded_global
from examples.pybullet.utils.pybullet_tools.panda_utils import get_arm_joints, ARM_NAMES, get_group_joints, \
    get_group_conf, get_group_links, BI_PANDA_GROUPS, arm_from_arm, TARGET, PLATE_GRASP_LEFT_ARM, TIME_STEP
from examples.pybullet.utils.pybullet_tools.utils import connect, get_pose, is_placement, disconnect, \
    get_joint_positions, HideOutput, LockRenderer, wait_for_user, get_max_limit, set_joint_positions_torque, set_point
from examples.pybullet.namo.stream import get_custom_limits

from pddlstream.algorithms.meta import create_parser, solve
from pddlstream.algorithms.common import SOLUTIONS
from pddlstream.language.generator import from_gen_fn, from_list_fn, from_fn, from_test
from pddlstream.language.constants import Equal, And, print_solution, Exists, get_args, is_parameter, \
    get_parameter_name, PDDLProblem
from pddlstream.utils import read, INF, get_file_path, Profiler
from pddlstream.language.function import FunctionInfo
from pddlstream.language.stream import StreamInfo, DEBUG

from examples.pybullet.utils.pybullet_tools.panda_primitives_v2 import apply_commands, State
from examples.pybullet.utils.pybullet_tools.utils import draw_base_limits, WorldSaver, has_gui, str_from_object, joint_from_name, is_pose_on_r, body_from_name, remove_fixed_constraint

from examples.pybullet.panda_force_aware.problems import PROBLEMS
from examples.pybullet.utils.pybullet_tools.panda_primitives_v2 import Pose, Conf, get_ik_ir_gen, get_motion_gen, \
    get_stable_gen, get_grasp_gen, Attach, Detach, Clean, Cook, control_commands, \
    get_gripper_joints, GripperCommand, apply_commands, State, FixObj, get_mass_global, set_mass_global
import time
import datetime
import pybullet as p
import csv
import numpy as np
import os

# TODO: collapse similar streams into a single stream when reodering

def get_bodies_from_type(problem):
    bodies_from_type = {}
    for body, ty in problem.body_types:
        bodies_from_type.setdefault(ty, set()).add(body)
    return bodies_from_type

def pddlstream_from_problem(problem, base_limits=None, collisions=True, teleport=False, name = None):
    robot = problem.robot

    domain_pddl = read(get_file_path(__file__, 'domain.pddl'))
    stream_pddl = read(get_file_path(__file__, 'stream.pddl'))
    constant_map = {
        '@sink': 'sink',
        '@stove': 'stove',
    }

    # initial_bq = Pose(robot, get_pose(robot))
    initial_bq = Conf(robot, get_group_joints(robot, 'base'), get_group_conf(robot, 'base'))
    init = [
        ('BConf', initial_bq),
        ('AtBConf', initial_bq),
        Equal(('PickCost',), 10),
        Equal(('PlaceCost',), 10),
        Equal(('ReconfigureCost',), 1),
    ] + [('Sink', s) for s in problem.sinks] + \
           [('Stove', s) for s in problem.stoves] + \
           [('Connected', b, d) for b, d in problem.buttons] + \
           [('Button', b) for b, _ in problem.buttons]
    for arm in ['right']:
    #for arm in problem.arms:
        joints = get_arm_joints(robot, arm)
        conf = Conf(robot, joints, get_joint_positions(robot, joints))
        init += [('Arm', arm), ('AConf', arm, conf), ('HandEmpty', arm), ('AtAConf', arm, conf), ('TorqueLimitsNotExceded', arm)]
        if arm in problem.arms:
            init += [('Controllable', arm)]
    for body in problem.movable:
        pose = Pose(body, get_pose(body), init=True) # TODO: supported here
        init += [('Graspable', body), ('Pose', body, pose),
                 ('AtPose', body, pose), ('Stackable', body, None)]
        for surface in problem.surfaces:
            if is_placement(body, surface):
                init += [('Supported', body, pose, surface)]
    for body, ty in problem.body_types:
        init += [('Type', body, ty)]

    bodies_from_type = get_bodies_from_type(problem)
    goal_literals = []
    # if problem.goal_conf is not None:
    #     goal_conf = Conf(robot, get_group_joints(robot, 'base'), problem.goal_conf)
    #     init += [('BConf', goal_conf)]
    #     goal_literals += [('AtBConf', goal_conf)]
    for ty, s in problem.goal_on:
        bodies = bodies_from_type[get_parameter_name(ty)] if is_parameter(ty) else [ty]
        init += [('Stackable', b, s) for b in bodies]
        goal_literals += [('On', ty, s)]
    goal_literals += [('Holding', a, b) for a, b in problem.goal_holding] + \
                     [('Cleaned', b)  for b in problem.goal_cleaned] + \
                     [('Cooked', b)  for b in problem.goal_cooked] + \
                    [('TorqueLimitsNotExceded', a) for a in problem.arms]
    goal_formula = []
    for literal in goal_literals:
        parameters = [a for a in get_args(literal) if is_parameter(a)]
        if parameters:
            type_literals = [('Type', p, get_parameter_name(p)) for p in parameters]
            goal_formula.append(Exists(parameters, And(literal, *type_literals)))
        else:
            goal_formula.append(literal)
    goal_formula = And(*goal_formula)

    custom_limits = {}
    if base_limits is not None:
        custom_limits.update(get_custom_limits(robot, problem.base_limits))



    stream_map = {
        'sample-pose': from_gen_fn(get_stable_gen_dumb(problem, collisions=collisions)),
        'sample-grasp': from_list_fn(get_grasp_gen(problem, collisions=collisions)),
        'test-cfree-pose-pose': from_test(get_cfree_pose_pose_test(collisions=collisions)),
        'test-cfree-approach-pose': from_test(get_cfree_approach_pose_test(problem, collisions=collisions)),
        'test-cfree-traj-pose': from_test(get_cfree_traj_pose_test(robot, collisions=collisions))
    }
    # if 'force_aware' in name:
    stream_map['sample-pose'] =  from_gen_fn(get_stable_gen_dumb(problem, collisions=collisions))
    # stream_map["inverse-kinematics"] = from_gen_fn(get_ik_ir_gen_no_reconfig(problem, custom_limits=custom_limits,
    #                                                     collisions=collisions, teleport=teleport))
    stream_map["inverse-kinematics"] = from_gen_fn(get_ik_ir_gen_force_aware(problem, custom_limits=custom_limits,
                                                        collisions=collisions, teleport=teleport))
    stream_map['test_torque_limits_not_exceded'] = from_test(get_torque_limits_not_exceded_test(problem))
    # elif name == 'bi_manual_forceful_ip':
    #     stream_map['sample-pose'] =  from_gen_fn(get_stable_gen(problem, collisions=collisions))
    #     stream_map["inverse-kinematics"] = from_gen_fn(get_ik_ir_gen_no_reconfig(problem, custom_limits=custom_limits,
    #                                                         collisions=collisions, teleport=teleport))
    #     stream_map['test_torque_limits_not_exceded'] = from_test(get_torque_limits_mock_test(problem))
    # elif name == 'bi_manual_forceful_reconfig':
    #     stream_map['sample-pose'] =  from_gen_fn(get_stable_gen_dumb(problem, collisions=collisions))
    #     stream_map["inverse-kinematics"] = from_gen_fn(get_ik_ir_gen(problem, custom_limits=custom_limits,
    #                                                         collisions=collisions, teleport=teleport))
    #     stream_map['test_torque_limits_not_exceded'] = from_test(get_torque_limits_not_exceded_test(problem))
    # else:
    #     stream_map['sample-pose'] =  from_gen_fn(get_stable_gen_dumb(problem, collisions=collisions))
    #     stream_map["inverse-kinematics"] = from_gen_fn(get_ik_ir_gen_no_reconfig(problem, custom_limits=custom_limits,
    #                                                         collisions=collisions, teleport=teleport))
    #     stream_map['test_torque_limits_not_exceded'] = from_test(get_torque_limits_mock_test(problem))
    #stream_map = DEBUG

    return PDDLProblem(domain_pddl, constant_map, stream_pddl, stream_map, init, goal_formula)

#######################################################
def post_process(problem, plan, teleport=False):
    reconfig_count = 0
    if plan is None:
        return None, reconfig_count
    commands = []
    for i, (name, args) in enumerate(plan):
        if name == 'move_base':
            c = args[-1]
            new_commands = c.commands
        elif name == 'pick':
            a, b, p, g, _, c = args
            trajs = c.commands
            close_gripper = GripperCommand(problem.robot, a, g.grasp_width, teleport=teleport)
            attach = Attach(problem.robot, a, g, b)
            if len(trajs) == 2:
                print("reconfig present")
                [t1, t2] = trajs
                reconfig_count += 1
                new_commands = [t1, t2, close_gripper, attach, t2.reverse()]
            else:
                print("no reconfig")
                [t2] = trajs
                # problem.extract_traj_data(t2)
                # problem.extract_traj_data(t2.reverse())
                new_commands = [t2, close_gripper, attach, t2.reverse()]
        elif name == 'place':
            a, b, p, g, _, c, _ = args
            trajectories = c.commands
            gripper_joint = get_gripper_joints(problem.robot, a)[0]
            position = 0.05
            open_gripper = GripperCommand(problem.robot, a, position, teleport=teleport)
            detach = Detach(problem.robot, a, b)
            if len(trajectories) == 2:
                print("reconfig present")
                [t1, t2] = c.commands
                reconfig_count+=1
                new_commands = [t1, t2, detach, open_gripper, t2.reverse()]
            else:
                print("no reconfig")
                [t2] = c.commands
                # problem.extract_traj_data(t2)
                # problem.extract_traj_data(t2.reverse())
                new_commands = [t2, detach, open_gripper, t2.reverse()]
        elif name == 'clean': # TODO: add text or change color?
            body, sink = args
            new_commands = [Clean(body)]
        elif name == 'cook':
            body, stove = args
            new_commands = [Cook(body)]
        elif name == 'press_clean':
            body, sink, arm, button, bq, c = args
            [t] = c.commands
            new_commands = [t, Clean(body), t.reverse()]
        elif name == 'press_cook':
            body, sink, arm, button, bq, c = args
            [t] = c.commands
            new_commands = [t, Cook(body), t.reverse()]
        else:
            raise ValueError(name)
        print(i, name, args, new_commands)
        commands += new_commands
    return commands, reconfig_count


def main(verbose=True):
    # TODO: could work just on postprocessing
    # TODO: try the other reachability database
    # TODO: option to only consider costs during local optimization


    parser = create_parser()
    parser.add_argument('-problem', default='packed_force_aware_transfer', help='The name of the problem to solve')
    parser.add_argument('-loops', default=10, type=int, help='The number of itterations to run experiment')
    parser.add_argument('-n', '--number', default=1, type=int, help='The number of objects')
    parser.add_argument('-cfree', action='store_true', help='Disables collisions')
    parser.add_argument('-deterministic', action='store_true', help='Uses a deterministic sampler')
    parser.add_argument('-optimal', action='store_true', help='Runs in an anytime mode')
    parser.add_argument('-t', '--max_time', default=400, type=int, help='The max time')
    parser.add_argument('-teleport', action='store_true', help='Teleports between configurations')
    parser.add_argument('-enable', action='store_true', help='Enables rendering during planning')
    parser.add_argument('-simulate', action='store_true', help='Simulates the system')
    args = parser.parse_args()
    print('Arguments:', args)
    DISTANCE = 0.5
    MASS = '9kg'
    data_dir = '/home/liam/success_rate_mass_data_random/'

    timestamp = str(datetime.datetime.now())
    f = f'{data_dir} {timestamp}_{args.problem}_{METHOD}'
    if 'dist' in data_dir:
        f = f'{f}_{DISTANCE}'
    if 'mass' in data_dir:
        f = f'{f}_{MASS}'
    os.mkdir(f)
    f += '/'
    datafileCsv = f + 'success_data.csv'
    header = ["TotalTime", "ExecutionTime", "Solved", "TotalItems", "TorquesExceded", "MassPerObject", "Method"]


    traj_file_base = f + timestamp + '_' + 'trajectory_data'

    with open(datafileCsv, 'w') as file:
        writer = csv.writer(file)
        writer.writerow(header)


    problem_fn_from_name = {fn.__name__: fn for fn in PROBLEMS}
    if args.problem not in problem_fn_from_name:
        raise ValueError(args.problem)

    problem_fn = problem_fn_from_name[args.problem]
    print('problem fn loaded')
    global torques_exceded

    for run in range(args.loops):
      traj_file = f'{traj_file_base}_{run}.npz'
      reset_torques_exceded_global()
      connect(use_gui=True)
      print('connected to gui')
      with HideOutput():
          problem = problem_fn(num=args.number, dist=DISTANCE)
          if any([not is_pose_on_r(get_pose(problem.movable[-1]), problem.surfaces[0])]):
            run = run - 1
            del problem
            disconnect()
            continue
      set_mass_global(get_mass(problem.movable[-1]))

      print('problem found')
      draw_base_limits(problem.base_limits, color=(1, 0, 0))
      saver = WorldSaver()
      print("world made")
      #handles = []
      #for link in get_group_joints(problem.robot, 'left_arm'):
      #    handles.append(draw_link_name(problem.robot, link))
      #wait_for_user()

      pddlstream_problem = pddlstream_from_problem(problem, collisions=not args.cfree, teleport=False, name=args.problem)
      stream_info = {
          'inverse-kinematics': StreamInfo(),
          'plan-base-motion': StreamInfo(overhead=1e1),
          'test_torque_limits_not_exceded': StreamInfo(p_success=1e-1),
          'test-cfree-pose-pose': StreamInfo(p_success=1e-3, verbose=verbose),
          'test-cfree-approach-pose': StreamInfo(p_success=1e-2, verbose=verbose),
          'test-cfree-traj-pose': StreamInfo(p_success=1e-1, verbose=verbose), # TODO: apply to arm and base trajs
          # 'test-forces-balanced': StreamInfo(p_success=1e-1, verbose=verbose),
          #'test-cfree-traj-grasp-pose': StreamInfo(verbose=verbose),

          # 'Distance': FunctionInfo(p_success=0.99, opt_fn=lambda q1, q2: 0),

          #'MoveCost': FunctionInfo(lambda t: BASE_CONSTANT),
      }
      #stream_info = {}

      _, _, _, stream_map, init, goal = pddlstream_problem
      print('Init:', init)
      print('Goal:', goal)
      print('Streams:', str_from_object(set(stream_map)))
      success_cost = 0 if args.optimal else INF
      planner = 'ff-astar' if args.optimal else 'ff-wastar3'
      search_sample_ratio = 2
      max_planner_time = 10
      # effort_weight = 0 if args.optimal else 1
      effort_weight = 1e-3 if args.optimal else 1
      start_time = time.time()
      with Profiler(field='tottime', num=25): # cumtime | tottime
          with LockRenderer(lock=not args.enable):
              solution = solve(pddlstream_problem, algorithm=args.algorithm, stream_info=stream_info,
                              planner=planner, max_planner_time=max_planner_time,
                              unit_costs=args.unit, success_cost=success_cost,
                              max_time=args.max_time, verbose=True, debug=True,
                              unit_efforts=True, effort_weight=effort_weight,
                              search_sample_ratio=search_sample_ratio)
              saver.restore()


      cost_over_time = [(s.cost, s.time) for s in SOLUTIONS]
      for i, (cost, runtime) in enumerate(cost_over_time):
          print('Plan: {} | Cost: {:.3f} | Time: {:.3f}'.format(i, cost, runtime))
      #print(SOLUTIONS)
      print_solution(solution)
      plan, cost, evaluations = solution
      if (plan is None) or not has_gui():
        total_time = time.time() - start_time
        exec_time = -1
        items = args.number
        solved = False
        torques_exceded = get_torques_exceded_global()
        mass = get_mass(problem.movable[-1])
        data = [total_time, exec_time, solved, items, torques_exceded, mass, METHOD]
        with open(datafileCsv, 'a') as file:
            writer = csv.writer(file)
            writer.writerow(data)
        # np.savez(traj_file, x=problem.solution_confs, y=problem.solution_vels, z=problem.solution_accels, xx=problem.solution_torques_rne, yy=problem.solution_torques_dyn, zz=problem.solution_torques_arne)
        disconnect()
        continue

      with LockRenderer(lock=not args.enable):
          problem.remove_gripper()
          commands, reconfig_count = post_process(problem, plan, teleport=args.teleport)
          saver.restore()
      p.setRealTimeSimulation(True)
    #   np.savez(traj_file, x=problem.solution_confs, y=problem.solution_vels, z=problem.solution_accels, xx=problem.solution_torques_rne, yy=problem.solution_torques_dyn, zz=problem.solution_torques_arne)
      draw_base_limits(problem.base_limits, color=(1, 0, 0))

      exec_time = time.time()
      state = State()
      if args.simulate:
          control_commands(commands)
      else:
        time_step = None if args.teleport else TIME_STEP
        state = apply_commands(state, commands, time_step)



        total_time = time.time() - start_time
        exec_time = time.time() - exec_time
        items = args.number
        solved = True
        mass = get_mass(problem.movable[0])
        torques_exceded = get_torques_exceded_global()
        data = [total_time, exec_time, solved, items, torques_exceded, mass, METHOD]
        with open(datafileCsv, 'a') as file:
            writer = csv.writer(file)
            # writer.writerow(header)
            writer.writerow(data)
      del problem
      del saver
      disconnect()

if __name__ == '__main__':
    main()