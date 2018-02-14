# Copyright (c) 2018 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

from UM.Logger import Logger

from UM.Settings.Interfaces import DefinitionContainerInterface
from UM.Settings.InstanceContainer import InstanceContainer
from UM.Settings.ContainerRegistry import ContainerRegistry
from UM.Util import parseBool

from .GlobalStack import GlobalStack
from .ExtruderStack import ExtruderStack
from typing import Optional


##  Contains helper functions to create new machines.
class CuraStackBuilder:
    ##  Create a new instance of a machine.
    #
    #   \param name The name of the new machine.
    #   \param definition_id The ID of the machine definition to use.
    #
    #   \return The new global stack or None if an error occurred.
    @classmethod
    def createMachine(cls, name: str, definition_id: str) -> Optional[GlobalStack]:
        from cura.CuraApplication import CuraApplication
        application = CuraApplication.getInstance()
        variant_manager = CuraApplication.getInstance()._variant_manager
        material_manager = CuraApplication.getInstance()._material_manager
        quality_manager = CuraApplication.getInstance()._quality_manager
        registry = ContainerRegistry.getInstance()

        definitions = registry.findDefinitionContainers(id = definition_id)
        if not definitions:
            Logger.log("w", "Definition {definition} was not found!", definition = definition_id)
            return None

        machine_definition = definitions[0]

        # get variant container for extruders
        variant_container = application.empty_variant_container
        # Only look for the preferred variant if this machine has variants
        variant_name = None
        if parseBool(machine_definition.getMetaDataEntry("has_variants", False)):
            variant_name = machine_definition.getMetaDataEntry("preferred_variant_name")
            if variant_name:
                variant_node = variant_manager.getVariantNode(definition_id, variant_name)
                # Sanity check. If you see this error, the related definition files should be fixed.
                if variant_node is None:
                    raise RuntimeError("Cannot find variant with definition [%s] and variant name [%s]" % (definition_id, variant_name))
                variant_container = variant_node.getContainer()

        # get material container for extruders
        material_container = application.empty_material_container
        # Only look for the preferred material if this machine has materials
        if parseBool(machine_definition.getMetaDataEntry("has_materials", False)):
            material_diameter = machine_definition.getProperty("material_diameter", "value")
            approximate_material_diameter = str(round(material_diameter))
            root_material_id = machine_definition.getMetaDataEntry("preferred_material")
            root_material_id = material_manager.getRootMaterialIDForDiameter(root_material_id, approximate_material_diameter)
            material_node = material_manager.getMaterialNode(definition_id, variant_name, material_diameter, root_material_id)
            # Sanity check. If you see this error, the related definition files should be fixed.
            if not material_node:
                raise RuntimeError("Cannot find material with definition [%s], variant_name [%s], and root_material_id [%s]" %
                                   (definition_id, variant_name, root_material_id))
            material_container = material_node.getContainer()

        generated_name = registry.createUniqueName("machine", "", name, machine_definition.getName())
        # Make sure the new name does not collide with any definition or (quality) profile
        # createUniqueName() only looks at other stacks, but not at definitions or quality profiles
        # Note that we don't go for uniqueName() immediately because that function matches with ignore_case set to true
        if registry.findContainersMetadata(id = generated_name):
            generated_name = registry.uniqueName(generated_name)

        new_global_stack = cls.createGlobalStack(
            new_stack_id = generated_name,
            definition = machine_definition,
            variant_container = application.empty_variant_container,  # TODO: fix for build plate
            material_container = application.empty_material_container,
            quality_container = application.empty_quality_container,
        )
        new_global_stack.setName(generated_name)

        # Create ExtruderStacks
        extruder_dict = machine_definition.getMetaDataEntry("machine_extruder_trains")

        for position, extruder_definition_id in extruder_dict.items():
            # Sanity check: make sure that the positions in the extruder definitions are same as in the machine
            # definition
            extruder_definition = registry.findDefinitionContainers(id = extruder_definition_id)[0]
            position_in_extruder_def = extruder_definition.getMetaDataEntry("position")
            if position_in_extruder_def != position:
                raise RuntimeError("Extruder position [%s] defined in extruder definition [%s] is not the same as in machine definition [%s] position [%s]" %
                                   (position_in_extruder_def, extruder_definition_id, definition_id, position))

            new_extruder_id = registry.uniqueName(extruder_definition_id)
            new_extruder = cls.createExtruderStack(
                new_extruder_id,
                extruder_definition = extruder_definition,
                machine_definition_id = definition_id,
                position = position,
                variant_container = variant_container,
                material_container = material_container,
                quality_container = application.empty_quality_container,
            )
            new_extruder.setNextStack(new_global_stack)
            new_global_stack.addExtruder(new_extruder)
            registry.addContainer(new_extruder)

        preferred_quality_type = machine_definition.getMetaDataEntry("preferred_quality_type")
        quality_group_dict = quality_manager.getQualityGroups(new_global_stack)
        quality_group = quality_group_dict.get(preferred_quality_type)

        new_global_stack.quality = quality_group.node_for_global.getContainer()
        for position, extruder_stack in new_global_stack.extruders.items():
            extruder_stack.quality = quality_group.nodes_for_extruders[position].getContainer()

        # Register the global stack after the extruder stacks are created. This prevents the registry from adding another
        # extruder stack because the global stack didn't have one yet (which is enforced since Cura 3.1).
        registry.addContainer(new_global_stack)

        return new_global_stack

    ##  Create a new Extruder stack
    #
    #   \param new_stack_id The ID of the new stack.
    #   \param definition The definition to base the new stack on.
    #   \param machine_definition_id The ID of the machine definition to use for
    #   the user container.
    #   \param kwargs You can add keyword arguments to specify IDs of containers to use for a specific type, for example "variant": "0.4mm"
    #
    #   \return A new Global stack instance with the specified parameters.
    @classmethod
    def createExtruderStack(cls, new_stack_id: str, extruder_definition: DefinitionContainerInterface, machine_definition_id: str,
                            position: int,
                            variant_container, material_container, quality_container) -> ExtruderStack:
        from cura.CuraApplication import CuraApplication
        application = CuraApplication.getInstance()

        stack = ExtruderStack(new_stack_id)
        stack.setName(extruder_definition.getName())
        stack.setDefinition(extruder_definition)

        stack.addMetaDataEntry("position", position)

        user_container = cls.createUserChangesContainer(new_stack_id + "_user", machine_definition_id, new_stack_id,
                                                        is_global_stack = False)

        stack.definitionChanges = cls.createDefinitionChangesContainer(stack, new_stack_id + "_settings")
        stack.variant = variant_container
        stack.material = material_container
        stack.quality = quality_container
        stack.qualityChanges = application.empty_quality_changes_container
        stack.userChanges = user_container

        # Only add the created containers to the registry after we have set all the other
        # properties. This makes the create operation more transactional, since any problems
        # setting properties will not result in incomplete containers being added.
        ContainerRegistry.getInstance().addContainer(user_container)

        return stack

    ##  Create a new Global stack
    #
    #   \param new_stack_id The ID of the new stack.
    #   \param definition The definition to base the new stack on.
    #   \param kwargs You can add keyword arguments to specify IDs of containers to use for a specific type, for example "variant": "0.4mm"
    #
    #   \return A new Global stack instance with the specified parameters.
    @classmethod
    def createGlobalStack(cls, new_stack_id: str, definition: DefinitionContainerInterface,
                          variant_container, material_container, quality_container) -> GlobalStack:
        from cura.CuraApplication import CuraApplication
        application = CuraApplication.getInstance()

        stack = GlobalStack(new_stack_id)
        stack.setDefinition(definition)

        # Create user container
        user_container = cls.createUserChangesContainer(new_stack_id + "_user", definition.getId(), new_stack_id,
                                                        is_global_stack = True)

        stack.definitionChanges = cls.createDefinitionChangesContainer(stack, new_stack_id + "_settings")
        stack.variant = variant_container
        stack.material = material_container
        stack.quality = quality_container
        stack.qualityChanges = application.empty_quality_changes_container
        stack.userChanges = user_container

        ContainerRegistry.getInstance().addContainer(user_container)

        return stack

    @classmethod
    def createUserChangesContainer(cls, container_name: str, definition_id: str, stack_id: str,
                                   is_global_stack: bool) -> "InstanceContainer":
        from cura.CuraApplication import CuraApplication

        unique_container_name = ContainerRegistry.getInstance().uniqueName(container_name)

        container = InstanceContainer(unique_container_name)
        container.setDefinition(definition_id)
        container.addMetaDataEntry("type", "user")
        container.addMetaDataEntry("setting_version", CuraApplication.SettingVersion)

        metadata_key_to_add = "machine" if is_global_stack else "extruder"
        container.addMetaDataEntry(metadata_key_to_add, stack_id)

        return container

    @classmethod
    def createDefinitionChangesContainer(cls, container_stack, container_name):
        from cura.CuraApplication import CuraApplication

        unique_container_name = ContainerRegistry.getInstance().uniqueName(container_name)

        definition_changes_container = InstanceContainer(unique_container_name)
        definition_changes_container.setDefinition(container_stack.getBottom().getId())
        definition_changes_container.addMetaDataEntry("type", "definition_changes")
        definition_changes_container.addMetaDataEntry("setting_version", CuraApplication.SettingVersion)

        ContainerRegistry.getInstance().addContainer(definition_changes_container)
        container_stack.definitionChanges = definition_changes_container

        return definition_changes_container
