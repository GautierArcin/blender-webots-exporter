# Copyright 1996-2018 Cyberbotics Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This Blender plugin script exports to the Webots format."""

import json
import os
import re

import bpy
import mathutils

from bpy_extras.io_utils import create_derived_objects, free_derived_objects


def export(file, global_matrix, scene, use_mesh_modifiers=False, use_selection=True, user_data={}, path_mode='AUTO'):
    """Export to wbt file."""

    # Global Setup
    import bpy_extras
    from bpy_extras.io_utils import unique_name
    from xml.sax.saxutils import escape

    # Caches
    uuid_cache_object = {}
    uuid_cache_mesh = {}
    uuid_cache_image = {}

    # Decorators
    COORDS_ = 'COORDS_'
    OB_ = 'OB_'
    ME_ = 'ME_'
    IM_ = 'IM_'
    GROUP_ = 'GROUP_'
    _IFS = '_IFS'
    _TRANSFORM = '_TRANSFORM'

    copy_set = set()  # Store files to copy.
    mesh_name_set = set()  # Store names of newly cerated meshes, so we dont overlap

    base_src = os.path.dirname(bpy.data.filepath)
    base_dst = os.path.dirname(file.name)

    def fw(line):
        strippedLine = line.strip()
        if strippedLine.startswith('}') or strippedLine.startswith(']'):
            fw.indentation -= 1
        if fw.isLastCharacterACariageReturn:
            file.write('  ' * fw.indentation)
        file.write(line)
        fw.isLastCharacterACariageReturn = line.endswith('\n')
        if strippedLine.endswith('{') or strippedLine.endswith('['):
            fw.indentation += 1
    fw.indentation = 0
    fw.isLastCharacterACariageReturn = False

    def write_header():
        fw('#VRML_SIM R2019a utf8\n')
        fw('WorldInfo {\n')
        fw('basicTimeStep 8\n')
        fw('}\n')
        fw('Viewpoint {\n')
        fw('orientation -0.5 -0.852 -0.159 0.71\n')
        fw('position -3.6 2.0 5.4\n')
        fw('}\n')
        fw('TexturedBackground {\n')
        fw('}\n')
        fw('TexturedBackgroundLight {\n')
        fw('}\n')

    def write_transform_begin(obj, matrix, def_id):
        loc, rot, sca = matrix.decompose()
        rot = rot.to_axis_angle()
        rot = (*rot[0], rot[1])

        hingeJoint = False
        exportBoundingObject = False
        exportPhysics = False
        if def_id in user_data:
            fw('DEF %s ' % def_id)
            node_data = user_data[def_id]
            fw('%s {\n' % node_data['webotsType'])
            hingeJoint = node_data['webotsType'] == 'HingeJoint'
            exportPhysics = node_data['webotsType'] != 'Robot'  # TODO: static root should be a parameter.
            exportBoundingObject = True
        elif nearly_equal(loc[0], 0.0) and nearly_equal(loc[1], 0.0) and nearly_equal(loc[2], 0.0) and nearly_equal(rot[3], 0.0) and \
                nearly_equal(sca[0], 1.0) and nearly_equal(sca[1], 1.0) and nearly_equal(sca[2], 1.0):
            return (True, False)  # Skipped useless transform.
        else:
            if def_id is not None:
                fw('DEF %s ' % def_id)
            fw('Transform {\n')

        if hingeJoint:
            node_data = user_data[def_id]
            fw('jointParameters HingeJointParameters {\n')
            fw('anchor %.6f %.6f %.6f\n' % loc[:])
            fw('axis %s\n' % node_data['hingeJointParameters']['axis'])
            fw('}\n')
            fw('device [\n')
            if 'motorName' in node_data:
                fw('RotationalMotor {\n')
                fw('name "%s"\n' % node_data['motorName'])
                fw('maxTorque 100000\n')
                fw('}\n')
            if 'positionSensorName' in node_data:
                fw('PositionSensor {\n')
                fw('name "%s"\n' % node_data['positionSensorName'])
                fw('}\n')
            fw(']\n')
            fw('endPoint Solid {\n')
            if 'motorName' in node_data:
                fw('name "%s"\n' % node_data['motorName'])

        fw('translation %.6f %.6f %.6f\n' % loc[:])
        fw('scale %.6f %.6f %.6f\n' % sca[:])
        fw('rotation %.6f %.6f %.6f %.6f\n' % rot)

        if exportPhysics:
            fw('physics Physics {\n')
            fw('}\n')
        if exportBoundingObject:
            fw('boundingObject Transform {\n')
            x = 0.5 * (max([v[0] for v in obj.bound_box]) + min([v[0] for v in obj.bound_box]))
            y = 0.5 * (max([v[1] for v in obj.bound_box]) + min([v[1] for v in obj.bound_box]))
            z = 0.5 * (max([v[2] for v in obj.bound_box]) + min([v[2] for v in obj.bound_box]))
            fw('translation %.6f %.6f %.6f\n' % (x, y, z))
            fw('children [\n')
            fw('Box {\n')
            fw('size %.6f %.6f %.6f\n' % obj.dimensions[:])
            fw('}\n')
            fw(']\n')
            fw('}\n')

        fw('children [\n')

        return (False, hingeJoint)

    def write_transform_end(supplementaryCurvyBracket):
        fw(']\n')
        fw('}\n')
        if supplementaryCurvyBracket:
            fw('}\n')

    def write_indexed_face_set(obj, mesh, matrix, world):
        obj_id = unique_name(obj, OB_ + obj.name, uuid_cache_object, clean_func=slugify, sep='_')
        mesh_id = unique_name(mesh, ME_ + mesh.name, uuid_cache_mesh, clean_func=slugify, sep='_')
        mesh_id_group = GROUP_ + mesh_id
        mesh_id_coords = COORDS_ + mesh_id

        # tessellation faces may not exist
        if not mesh.tessfaces and mesh.polygons:
            mesh.update(calc_tessface=True)

        if not mesh.tessfaces:
            return

        # use _ifs_TRANSFORM suffix so we dont collide with transform node when hierarchys are used.
        (skipUselessTransform, supplementaryCurvyBracket) = write_transform_begin(obj, matrix, obj_id + _IFS + _TRANSFORM)

        if mesh.tag:
            fw('USE %s\n' % (mesh_id_group))
        else:
            mesh.tag = True

            is_uv = bool(mesh.tessface_uv_textures.active)
            is_coords_written = False

            mesh_materials = mesh.materials[:]
            if not mesh_materials:
                mesh_materials = [None]

            mesh_material_tex = [None] * len(mesh_materials)
            mesh_material_mtex = [None] * len(mesh_materials)
            mesh_material_images = [None] * len(mesh_materials)

            for i, material in enumerate(mesh_materials):
                if material:
                    for mtex in material.texture_slots:
                        if mtex:
                            tex = mtex.texture
                            if tex and tex.type == 'IMAGE':
                                image = tex.image
                                if image:
                                    mesh_material_tex[i] = tex
                                    mesh_material_mtex[i] = mtex
                                    mesh_material_images[i] = image
                                    break

            mesh_materials_use_face_texture = [getattr(material, 'use_face_texture', True) for material in mesh_materials]

            mesh_faces = mesh.tessfaces[:]
            mesh_faces_materials = [f.material_index for f in mesh_faces]
            mesh_faces_vertices = [f.vertices[:] for f in mesh_faces]

            if is_uv and True in mesh_materials_use_face_texture:
                mesh_faces_image = [
                    (fuv.image if mesh_materials_use_face_texture[mesh_faces_materials[i]]
                        else mesh_material_images[mesh_faces_materials[i]])
                    for i, fuv in enumerate(mesh.tessface_uv_textures.active.data)
                ]

                mesh_faces_image_unique = set(mesh_faces_image)
            elif len(set(mesh_material_images) | {None}) > 1:  # Make sure there is at least one image
                mesh_faces_image = [mesh_material_images[material_index] for material_index in mesh_faces_materials]
                mesh_faces_image_unique = set(mesh_faces_image)
            else:
                mesh_faces_image = [None] * len(mesh_faces)
                mesh_faces_image_unique = {None}

            # Group faces.
            face_groups = {}
            for material_index in range(len(mesh_materials)):
                for image in mesh_faces_image_unique:
                    face_groups[material_index, image] = []
            del mesh_faces_image_unique

            for i, (material_index, image) in enumerate(zip(mesh_faces_materials, mesh_faces_image)):
                face_groups[material_index, image].append(i)

            # Same as face_groups.items() but sorted so we can get predictable output.
            face_groups_items = list(face_groups.items())
            face_groups_items.sort(key=lambda m: (m[0][0], getattr(m[0][1], 'name', '')))

            for (material_index, image), face_group in face_groups_items:  # face_groups.items()
                if face_group:
                    material = mesh_materials[material_index]

                    fw('Shape {\n')

                    is_smooth = False

                    for i in face_group:
                        if mesh_faces[i].use_smooth:
                            is_smooth = True
                            break

                    fw('appearance PBRAppearance {\n')

                    if image:
                        write_image_texture(image)

                    if material:
                        diffuse = material.diffuse_color[:]
                        ambient = ((material.ambient * 2.0) * world.ambient_color)[:] if world else [0.0, 0.0, 0.0]
                        emissive = tuple(((c * material.emit) + ambient[i]) / 2.0 for i, c in enumerate(diffuse))

                        fw('baseColor %.3f %.3f %.3f\n' % clamp_color(diffuse))
                        fw('emissiveColor %.3f %.3f %.3f\n' % clamp_color(emissive))
                        fw('metalness 0\n')
                        fw('roughness 0.5\n')

                    fw('}\n')  # -- PBRAppearance

                    mesh_faces_uv = mesh.tessface_uv_textures.active.data if is_uv else None

                    fw('geometry IndexedFaceSet {\n')

                    if is_smooth:
                        # use Auto-Smooth angle, if enabled. Otherwise make
                        # the mesh perfectly smooth by creaseAngle > pi.
                        fw('creaseAngle %.4f\n' % (mesh.auto_smooth_angle if mesh.use_auto_smooth else 1.0))

                    # for IndexedTriangleSet we use a uv per vertex so this isnt needed.
                    if is_uv:
                        fw('texCoordIndex [\n')

                        j = 0
                        for i in face_group:
                            if len(mesh_faces_vertices[i]) == 4:
                                fw('%d %d %d %d -1 ' % (j, j + 1, j + 2, j + 3))
                                j += 4
                            else:
                                fw('%d %d %d -1 ' % (j, j + 1, j + 2))
                                j += 3
                        fw('\n')
                        fw(']\n')

                    if True:
                        fw('coordIndex [\n')
                        for i in face_group:
                            fv = mesh_faces_vertices[i]
                            if len(fv) == 3:
                                fw('%i %i %i -1 ' % fv)
                            else:
                                fw('%i %i %i %i -1 ' % fv)
                        fw('\n')
                        fw(']\n')

                    if True:
                        if is_coords_written:
                            fw('coord USE %s\n' % (mesh_id_coords))
                        else:
                            fw('coord ')
                            fw('DEF %s ' % mesh_id_coords)
                            fw('Coordinate {\n')
                            fw('point [\n')
                            for v in mesh.vertices:
                                fw('%.6f %.6f %.6f ' % v.co[:])
                            fw('\n')
                            fw(']\n')
                            fw('}\n')

                            is_coords_written = True

                    if is_uv:
                        fw('texCoord TextureCoordinate {\n')
                        fw('point [\n')
                        for i in face_group:
                            for uv in mesh_faces_uv[i].uv:
                                fw('%.4f %.4f ' % uv[:])
                        del mesh_faces_uv
                        fw('\n')
                        fw(']\n')
                        fw('}\n')

                    fw('}\n')  # --- IndexedFaceSet
                    fw('}\n')  # --- Shape

        if not skipUselessTransform:
            write_transform_end(supplementaryCurvyBracket)

    def write_image_texture(image):
        image_id = unique_name(image, IM_ + image.name, uuid_cache_image, clean_func=slugify, sep='_')

        if image.tag:
            fw('texture USE %s\n' % (image_id))
        else:
            image.tag = True

            fw('texture ')
            fw('DEF %s ' % image_id)
            fw('ImageTexture {\n')

            # Collect image paths, can load multiple [relative, name-only, absolute]
            filepath = image.filepath
            filepath_full = bpy.path.abspath(filepath, library=image.library)
            filepath_ref = bpy_extras.io_utils.path_reference(filepath_full, base_src, base_dst, path_mode, 'textures', copy_set, image.library)
            filepath_base = os.path.basename(filepath_full)

            images = [
                filepath_ref,
                filepath_base,
            ]
            if path_mode != 'RELATIVE':
                images.append(filepath_full)

            images = [f.replace('\\', '/') for f in images]
            images = [f for i, f in enumerate(images) if f not in images[:i]]

            fw('url [ "%s" ]\n' % ' '.join(['"%s"' % escape(f) for f in images]))
            fw('}\n')

    def export_object(obj_main_parent, obj_main, obj_children):
        """Export Object Hierarchy (recursively called)."""
        matrix_fallback = mathutils.Matrix()
        world = scene.world
        free, derived = create_derived_objects(scene, obj_main)

        obj_main_matrix_world = obj_main.matrix_world
        if obj_main_parent:
            obj_main_matrix = obj_main_parent.matrix_world.inverted(matrix_fallback) * obj_main_matrix_world
        else:
            obj_main_matrix = obj_main_matrix_world
        obj_main_matrix_world_invert = obj_main_matrix_world.inverted(matrix_fallback)

        obj_main_id = unique_name(obj_main, obj_main.name, uuid_cache_object, clean_func=slugify, sep='_')

        (skipUselessTransform, supplementaryCurvyBracket) = write_transform_begin(obj_main, obj_main_matrix if obj_main_parent else global_matrix * obj_main_matrix, obj_main_id + _TRANSFORM)

        for obj, obj_matrix in (() if derived is None else derived):
            obj_type = obj.type
            obj_matrix = obj_main_matrix_world_invert * obj_matrix  # Make transform node relative.

            if obj_type in {'MESH', 'CURVE', 'SURFACE', 'FONT'}:
                if (obj_type != 'MESH') or (use_mesh_modifiers and obj.is_modified(scene, 'PREVIEW')):
                    me = obj.to_mesh(scene, use_mesh_modifiers, 'PREVIEW')
                    do_remove = True
                else:
                    me = obj.data
                    do_remove = False

                if me is not None:
                    # ensure unique name, we could also do this by
                    # postponing mesh removal, but clearing data - TODO
                    if do_remove:
                        me.name = obj.name.rstrip('1234567890').rstrip('.')
                        me_name_new = me_name_org = me.name
                        count = 0
                        while me_name_new in mesh_name_set:
                            me.name = '%.17s.%03d' % (me_name_org, count)
                            me_name_new = me.name
                            count += 1
                        mesh_name_set.add(me_name_new)
                        del me_name_new, me_name_org, count

                    write_indexed_face_set(obj, me, obj_matrix, world)

                    # Rree mesh created with create_mesh()
                    if do_remove:
                        bpy.data.meshes.remove(me)

            else:
                # print('Info: Ignoring [%s], object type [%s] not handle yet' % (object.name,object.getType))
                pass

        if free:
            free_derived_objects(obj_main)

        # Write out children recursively
        for obj_child, obj_child_children in obj_children:
            export_object(obj_main, obj_child, obj_child_children)

        if not skipUselessTransform:
            write_transform_end(supplementaryCurvyBracket)

    def export_main():
        """Main Export Function."""

        # tag un-exported IDs
        bpy.data.meshes.tag(False)
        bpy.data.materials.tag(False)
        bpy.data.images.tag(False)

        if use_selection:
            objects = [obj for obj in scene.objects if obj.is_visible(scene) and obj.select]
        else:
            objects = [obj for obj in scene.objects if obj.is_visible(scene)]

        print('Info: starting Webots export to %r...' % file.name)
        write_header()

        objects_hierarchy = build_hierarchy(objects)

        for obj_main, obj_main_children in objects_hierarchy:
            export_object(None, obj_main, obj_main_children)

    export_main()

    # Global cleanup
    file.close()

    # Copy all collected files.
    bpy_extras.io_utils.path_reference_copy(copy_set)

    print('Info: finished Webots export to %r' % file.name)


def save(context, filepath, *, use_selection=True, use_mesh_modifiers=False, user_data_path='', global_matrix=None, path_mode='AUTO'):
    bpy.path.ensure_ext(filepath, '.wbt')
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode='OBJECT')

    file = open(filepath, 'w', encoding='utf-8')

    if global_matrix is None:
        global_matrix = mathutils.Matrix()

    user_data = {}
    if user_data_path and os.path.isfile(user_data_path):
        with open(user_data_path) as f:
            user_data = json.load(f)

    export(file, global_matrix, context.scene, use_mesh_modifiers=use_mesh_modifiers, use_selection=use_selection, user_data=user_data, path_mode=path_mode)

    return {'FINISHED'}


def clamp_color(col):
    return tuple([max(min(c, 1.0), 0.0) for c in col])


def matrix_direction_neg_z(matrix):
    return (matrix.to_3x3() * mathutils.Vector((0.0, 0.0, -1.0))).normalized()[:]


def bool_as_str(value):
    return ('FALSE', 'TRUE')[bool(value)]


def slugify(s):
    if not s:
        s = 'none'
    s = s.upper()
    for k in range(len(s)):
        if not re.match(r'[A-Z]', s[k]):
            s = s[:k] + '_' + s[(k + 1):]
    if not re.match(r'[A-Z]', s[0]):
        s = '_' + s
    while '__' in s:
        s = s.replace('__', '_')
    if s[-1] == '_':
        s = s[:-1]
    return s


def nearly_equal(a, b, sig_fig=5):
    return a == b or int(a * 10 ** sig_fig) == int(b * 10 ** sig_fig)


def build_hierarchy(objects):
    """Returns parent child relationships, skipping."""
    objects_set = set(objects)
    par_lookup = {}

    def test_parent(parent):
        while (parent is not None) and (parent not in objects_set):
            parent = parent.parent
        return parent

    for obj in objects:
        par_lookup.setdefault(test_parent(obj.parent), []).append((obj, []))

    for parent, children in par_lookup.items():
        for obj, subchildren in children:
            subchildren[:] = par_lookup.get(obj, [])

    return par_lookup.get(None, [])
