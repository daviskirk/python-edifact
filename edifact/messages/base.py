# -*- coding: utf-8 -*-
"""This module provides the base for message wrappers."""

import os

import six

import xml.etree.ElementTree as ET
import random
import string

from edifact.helpers import separate_segments, separate_components, validate_anchor_segments
from edifact.exceptions import MissingSegmentAtPositionError
# from edifact.configuration import SEGMENT_CLASSES

import logging
edifact_logger = logging.getLogger('edifact')


class MessageMeta(type):
    """Meta class for segments."""

    def __new__(cls, name, bases, attrs):
        """Create class."""
        if 'Meta' in attrs:
            attrs = generate_structure(attrs)
        return super(MessageMeta, cls).__new__(cls, name, bases, attrs)


def generate_structure(attrs):
    xml_spec = get_xml_spec(attrs['Meta'])

    attrs['elements'] = []

    for child in xml_spec.getroot():
        attrs['elements'].append(process_element(child))

    return attrs


def process_element(element):
    if element.tag == 'GROUP':
        return process_group(element)
    else:
        return PlaceholderSegment(element.tag, **element.attrib)


def process_group(element):
    elements = []
    for child in element:
        elements.append(process_element(child))
    return SegmentGroup(elements, **element.attrib)


def get_xml_spec(meta):
    spec_dir = meta.spec_dir if hasattr(meta, 'spec_dir') else os.path.dirname(__file__) + '/specs'
    return ET.parse('{spec_dir}/{spec}.xml'.format(spec_dir=spec_dir, spec=meta.spec))


class Message(six.with_metaclass(MessageMeta)):
    """Base class for segments."""

    total_number_of_segments = 31

    data = {}

    def __init__(self, una, src_string=None):
        """Constructor."""
        if src_string:
            self.initialize_from_src_string(src_string)

    def initialize_from_src_string(self, src_string):
        segments = [separate_components(segment) for segment in separate_segments(src_string)]
        validate_anchor_segments(segments)
        self.process_segments(segments, 0, [0], 0)

    def process_segments(self, segments, segment_index, elements_indices, repeats, last_containers=[]):
        edifact_logger.debug('------------------------- process segment (%s/%s) ----------------------' % (segment_index, len(segments),))
        edifact_logger.debug('element indices: ' + ', '.join([unicode(ei) for ei in elements_indices]) + ', repeats: %s' % repeats)

        # Exit at the end of segments or elements
        if segment_index >= len(segments) or len(elements_indices) < 1:
            edifact_logger.debug('end of segments')
            return

        # Skip certain segments
        segments_to_ignore = ['UNA', 'UNH', 'BGM', 'UNT']
        tag = segments[segment_index][0]
        if tag in segments_to_ignore:
            edifact_logger.debug('ignoring segment %s with tag %s' % (segment_index, tag,))
            self.process_segments(
                segments, segment_index + 1, elements_indices, repeats, last_containers
            )
            return

        # Process segment
        segment_or_group = self.get_element(elements_indices)
        edifact_logger.debug('segment %s -> %s' % (segment_index, segments[segment_index],))

        # End of group or hierarchy
        if segment_or_group is None:
            edifact_logger.debug('element is None')
            segment_index, elements_indices, repeats, last_containers = self.process_end(
                segments, segment_index, elements_indices, repeats, last_containers
            )

        # Process group
        if isinstance(segment_or_group, SegmentGroup):
            edifact_logger.debug('element is a group -> %s' % segment_or_group.label)
            segment_index, elements_indices, repeats, last_containers = self.process_group(
                segments, segment_index, elements_indices, repeats, last_containers
            )

        # Process segment
        if isinstance(segment_or_group, PlaceholderSegment):
            edifact_logger.debug('element is a segment')
            segment_index, elements_indices, repeats, last_containers = self.process_segment(
                segments, segment_index, elements_indices, repeats, last_containers
            )

        # Proceed
        self.process_segments(segments, segment_index, elements_indices, repeats, last_containers)

    def process_end(self, segments, segment_index, elements_indices, repeats, last_containers):
        # Move one level up
        elements_indices = elements_indices[:-1]

        # Reset a possible repeat cycle
        repeats = 0

        # Void last container, unless empty
        if len(last_containers) > 0:
            del last_containers[-1]
            edifact_logger.debug('contracted groups %s' % ', '.join([c.label for c in last_containers]))

        # Return
        return segment_index, elements_indices, repeats, last_containers

    def process_group(self, segments, segment_index, elements_indices, repeats, last_containers):
        tag = segments[segment_index][0]
        group = self.get_element(elements_indices)

        # If repeats are exhausted for this group, we stop right there
        if repeats + 1 >= group.repeats:
            return self.process_end(segments, segment_index, elements_indices, repeats, last_containers)
        else:
            repeats += 1

        # Group does not have current segment as first segment
        if not group_starts_with_segment(group, tag):

            # Crash is group is mandatory and this is not a repeat attempt
            if group.mandatory and repeats == 0:
                raise MissingSegmentAtPositionError(group.get(0).tag)

            # Otherwise move on
            else:
                repeats = 0
                elements_indices[-1] += 1

        # Create and enter group
        else:
            last_containers.append(self.add_group(group, last_containers, repeats))
            edifact_logger.debug('expanded groups %s' % ', '.join([c.label for c in last_containers]))
            elements_indices.append(0)

        # Return
        return segment_index, elements_indices, repeats, last_containers

    def process_segment(self, segments, segment_index, elements_indices, repeats, last_containers):
        tag = segments[segment_index][0]
        element = self.get_element(elements_indices)

        # Tags don't match
        if not element.tag == tag:

            # Crash if segment is mandatory and this is not a repeat attempt
            if element.mandatory and repeats == 0:
                raise MissingSegmentAtPositionError(element.tag)

            # Otherwise move on
            else:
                repeats = 0
                elements_indices[-1] += 1

        # Process matching tags
        else:
            # Add segment to instance
            self.add_segment(element, segments[segment_index], last_containers)

            # Move to next segment
            segment_index += 1

            # Move on to next element if repeats are exhausted, otherwise repeat
            edifact_logger.debug('check repeats: %s' % repeats)
            if repeats + 1 >= element.repeats:
                elements_indices[-1] += 1
                repeats = 0
            else:
                repeats += 1

        # Return
        return segment_index, elements_indices, repeats, last_containers

    def add_group(self, placeholder_group, parent_groups, repeats):
        group = Group(**placeholder_group.__dict__())
        edifact_logger.debug('created new group %s' % group.label)

        if parent_groups is not None and len(parent_groups) > 0:
            parent_groups[-1].add(group.label, group)
            edifact_logger.debug('added group %s with uid %s to parent group %s' % (group.label, group.uid, parent_groups[-1].label,))
        else:
            self.add_to_data(group.label, group)
            edifact_logger.debug('added group %s with uid %s to root' % (group.label, group.uid,))

        return group

    def add_segment(self, element, segment_data, parent_groups):
        segment = DummySegment(segment_data[1:], **element.__dict__())
        edifact_logger.debug('new segment with data: %s' % segment_data)

        if parent_groups is not None and len(parent_groups) > 0:
            parent_groups[-1].add(segment.label, segment)
            edifact_logger.debug('added segment %s to group %s with uid %s' % (segment.label, parent_groups[-1].label, parent_groups[-1].uid,))
        else:
            self.add_to_data(segment.label, segment)
            edifact_logger.debug('added segment %s to root' % segment.label)

    def get_element(self, elements_indices):
        if elements_indices[0] >= len(self.elements):
            return None

        result = self.elements[elements_indices[0]]
        for idx in elements_indices[1:]:
            try:
                result = result.get(idx)
            except IndexError:
                result = None
        return result

    def add_to_data(self, label, element):
        if label not in self.data:
            self.data[label] = []
        self.data[label].append(element)

    # Magic
    def __getitem__(self, key):
        return self.data[key]


def group_starts_with_segment(group, tag):
    return group.get(0).tag == tag


# Just for now
class PlaceholderSegment(object):
    def __init__(self, tag, status='M', repeats='1', label=None, description=None):
        self.tag = tag
        self.mandatory = status == 'M'
        self.repeats = int(repeats)
        self.label = label
        self.description = description

    def __dict__(self):
        return {
            'tag': self.tag,
            'label': self.label,
            'description': self.description
        }


class SegmentGroup(object):
    def __init__(self, elements, status='M', repeats='1', label=None, description=None):
        self.elements = elements
        self.mandatory = status == 'M'
        self.repeats = int(repeats)
        self.label = label
        self.description = description

    def get(self, index):
        return self.elements[index]

    def __dict__(self):
        return {
            'mandatory': self.mandatory,
            'repeats': self.repeats,
            'label': self.label,
            'description': self.description
        }


class Group(object):
    def __init__(self, mandatory=False, repeats=1, label=None, description=None, repeat=0):
        self.mandatory = mandatory
        self.repeats = repeats
        self.label = label
        self.description = description
        self.repeat = repeat
        self.uid = ''.join(random.choice(string.lowercase) for i in range(5))

    def add(self, label, element):
        if not hasattr(self, 'elements'):
            self.elements = {}
        if label not in self.elements:
            self.elements[label] = []
        self.elements[label].append(element)
        edifact_logger.debug('group %s (%s) added element %s, elements are now %s' % (self.label, self.uid, element.label, len(self.elements),))


# Mock
class DummySegment(object):
    def __init__(self, data, tag=None, label=None, description=None):
        self.data = data
        self.tag = tag
        self.label = label if label is not None else tag
        self.description = description
