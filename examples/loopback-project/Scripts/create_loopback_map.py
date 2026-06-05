"""Create the loopback test map programmatically.

Replaces the "map creation manual" gap from the v1 examples: builds
/Game/Maps/Loopback with a floor, a PlayerStart, and one pickup actor, then saves it.

Run it one of two ways:

  # Headless / from a launch arg:
  UnrealEditor.exe LoopbackProject.uproject \
    -ExecCmds="py \"<path>/create_loopback_map.py\"" -unattended

  # Or via the agent bridge (editor already running):
  exec_python { code: open(".../create_loopback_map.py").read() }

Idempotent: re-running overwrites the same map package.
"""

import unreal

MAP_PATH = "/Game/Maps/Loopback"
PICKUP_CLASS_PATH = "/Game/Blueprints/BP_LoopbackPickup.BP_LoopbackPickup_C"


def _level_subsystem() -> unreal.LevelEditorSubsystem:
    return unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)


def _actor_subsystem() -> unreal.EditorActorSubsystem:
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def _spawn(cls, location, label):
    actor = _actor_subsystem().spawn_actor_from_class(cls, unreal.Vector(*location))
    if actor:
        actor.set_actor_label(label)
    return actor


def main() -> None:
    level_ss = _level_subsystem()

    # Fresh empty level at the target package path.
    level_ss.new_level(MAP_PATH)

    # Floor: a scaled engine plane so the player has something to stand on.
    plane = unreal.load_object(None, "/Engine/BasicShapes/Plane.Plane")
    floor = _spawn(unreal.StaticMeshActor, (0.0, 0.0, 0.0), "Loopback_Floor")
    if floor and plane:
        floor.static_mesh_component.set_static_mesh(plane)
        floor.set_actor_scale3d(unreal.Vector(20.0, 20.0, 1.0))
        floor.set_mobility(unreal.ComponentMobility.STATIC)

    # PlayerStart so PIE has a spawn point.
    _spawn(unreal.PlayerStart, (0.0, 0.0, 100.0), "Loopback_PlayerStart")

    # One pickup actor for the canonical "drive in, collect, assert" loop.
    pickup_cls = unreal.load_class(None, PICKUP_CLASS_PATH)
    if pickup_cls:
        _spawn(pickup_cls, (300.0, 0.0, 50.0), "Loopback_Pickup")
    else:
        unreal.log_warning(
            "BP_LoopbackPickup not found at %s; map created without a pickup." % PICKUP_CLASS_PATH
        )

    level_ss.save_current_level()
    unreal.log("Loopback map created and saved at %s" % MAP_PATH)


if __name__ == "__main__":
    main()
