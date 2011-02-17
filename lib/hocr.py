# encoding=UTF-8
# Copyright © 2008, 2009, 2010, 2011 Jakub Wilk <jwilk@jwilk.net>
#
# This package is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 dated June, 1991.
#
# This package is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.

'''
Process hOCR documents.

The hOCR format specification:
http://docs.google.com/Doc?id=dfxcv4vc_67g844kf
'''

import functools
import re

from . import utils

try:
    from lxml import etree
except ImportError, ex:
    utils.enhance_import_error(ex, 'lxml', 'python-lxml', 'http://codespeak.net/lxml/')
    raise

from . import errors
from . import text_zones
from . import unicode_support

const = text_zones.const

TEXT_DETAILS_LINE = const.TEXT_ZONE_LINE
TEXT_DETAILS_WORD = const.TEXT_ZONE_WORD
TEXT_DETAILS_CHARACTER = const.TEXT_ZONE_CHARACTER

hocr_class_to_djvu = \
dict(
    ocr_page=const.TEXT_ZONE_PAGE,
    ocr_column=const.TEXT_ZONE_COLUMN,
    ocr_carea=const.TEXT_ZONE_COLUMN,
    ocr_par=const.TEXT_ZONE_PARAGRAPH,
    ocr_line=const.TEXT_ZONE_LINE,
    ocr_word=const.TEXT_ZONE_WORD,
    ocrx_block=const.TEXT_ZONE_REGION,
    ocrx_line=const.TEXT_ZONE_LINE,
    ocrx_word=const.TEXT_ZONE_WORD
).get

cuneiform_tag_to_djvu = \
dict(
    body=const.TEXT_ZONE_PAGE,
    p=const.TEXT_ZONE_PARAGRAPH,
    span=const.TEXT_ZONE_CHARACTER,
).get

_djvu_zone_to_hocr = {
    const.TEXT_ZONE_PAGE: ('div', 'ocr_page'),
    const.TEXT_ZONE_COLUMN: ('div', 'ocr_carea'),
    const.TEXT_ZONE_PARAGRAPH: ('p', 'ocr_par'),
    const.TEXT_ZONE_REGION: ('span', 'ocrx_block'),
    const.TEXT_ZONE_LINE: ('span', 'ocrx_line'),
    const.TEXT_ZONE_WORD: ('span', 'ocrx_word'),
}
djvu2hocr_capabilities = list(sorted(cls for tag, cls in _djvu_zone_to_hocr.itervalues()))
djvu_zone_to_hocr = _djvu_zone_to_hocr.__getitem__
del _djvu_zone_to_hocr

bbox_re = re.compile(
    r'''
        bbox \s+
        (?P<x0> -?\d+) \s+
        (?P<y0> -?\d+) \s+
        (?P<x1> -?\d+) \s+
        (?P<y1> -?\d+)
    ''', re.VERBOSE)

bboxes_re = re.compile(
    r'''
        bboxes \s+
        (          (?: -?\d+ \s+ -?\d+ \s+ -?\d+ \s+ -?\d+)
        (?: ,? \s* (?: -?\d+ \s+ -?\d+ \s+ -?\d+ \s+ -?\d+) )* )
    ''', re.VERBOSE)

def _apply_bboxes(djvu_class, title, text, settings):
    embedded_eol = False
    if djvu_class <= const.TEXT_ZONE_LINE:
        if text.endswith('\n'):
            embedded_eol = True
    # Cuneiform tends to attach superfluous whitespace.
    # Also, a newline character can appear at the end of line.
    new_text = text.rstrip()
    trailing_whitespace_len = len(text) - len(new_text)
    text = new_text
    del new_text
    if settings.details >= djvu_class:
        return [text]
    m = bboxes_re.search(title)
    if not m:
        return [text]
    coordinates = (int(x) for x in m.group(1).replace(',', ' ').split())
    coordinates = zip(coordinates, coordinates, coordinates, coordinates)
    if len(coordinates) == len(text):
        pass  # OK
    else:
        if 0 < len(coordinates) - len(text) <= trailing_whitespace_len:
            # Cuneiform ≥ 0.9 provides bounding boxes for some whitespace characters.
            # Also, a newline character can appear at the end of line.
            del coordinates[len(text):]
        elif not settings.cuneiform and not embedded_eol and len(coordinates) == len(text) + 1:
            # OCRopus produces weird hOCR output if line ends with a hyphen.
            del coordinates[-1]
        else:
            raise errors.MalformedHocr("number of bboxes doesn't match text length")
    assert len(coordinates) == len(text)
    if djvu_class > const.TEXT_ZONE_WORD:
        # Split words
        words = []
        break_iterator = unicode_support.word_break_iterator(text, locale=settings.uax29)
        i = 0
        for j in break_iterator:
            subtext = text[i:j]
            if subtext.isspace():
                i = j
                continue
            bbox = text_zones.BBox()
            for k in xrange(i, j):
                if settings.cuneiform and coordinates[k] == (-1, -1, -1, -1):
                    raise errors.MalformedHocr("missing bbox for non-whitespace character")
                bbox.update(text_zones.BBox(*coordinates[k]))
            last_word = text_zones.Zone(type=const.TEXT_ZONE_WORD, bbox=bbox)
            words += last_word,
            if settings.details > TEXT_DETAILS_CHARACTER:
                last_word += subtext,
            else:
                last_word += [
                    text_zones.Zone(type=const.TEXT_ZONE_CHARACTER, bbox=(x0, y0, x1, y1), children=[ch])
                    for k in xrange(i, j)
                    for (x0, y0, x1, y1), ch in [(coordinates[k], text[k])]
                ]
            i = j
        return words
    else:
        # Split characters
        return [
            text_zones.Zone(type=const.TEXT_ZONE_CHARACTER, bbox=(x0, y0, x1, y1), children=[ch])
            for (x0, y0, x1, y1), ch in zip(coordinates, text)
        ]
    return [text]

def _scan(node, settings):

    def get_children(node):
        result = []
        if node.text:
            result += [node.text]
        for child in node.iterchildren():
            result += _scan(child, settings)
            if child.tail:
                result += [child.tail]
        return result

    if not isinstance(node.tag, basestring):
        # Ignore non-elements.
        return
    if settings.cuneiform and settings.cuneiform <= (0, 8):
        # Cuneiform ≤ 0.8 don't mark OCR elements in an hOCR way.
        djvu_class = cuneiform_tag_to_djvu(node.tag)
    else:
        hocr_classes = (node.get('class') or '').split()
        djvu_class = None
        for hocr_class in hocr_classes:
            djvu_class = hocr_class_to_djvu(hocr_class)
            if djvu_class:
                break
        else:
            if node.tag == 'p':
                # Cuneiform ≥ 0.9 don't mark paragraphs in an hOCR way.
                djvu_class = cuneiform_tag_to_djvu(node.tag)

    if not djvu_class:
        # Just process our children.
        return get_children(node)
    title = node.get('title') or ''
    m = bbox_re.search(title)
    if m is None:
        bbox = text_zones.BBox()
    else:
        bbox = text_zones.BBox(
            *(int(m.group(ident))
            for ident in ('x0', 'y0', 'x1', 'y1'))
        )

    if djvu_class is const.TEXT_ZONE_PAGE:
        if not bbox:
            if settings.page_size is None:
                raise errors.MalformedHocr("page without bounding box information")
            page_width, page_height = settings.page_size
            bbox = text_zones.BBox(0, 0, page_width, page_height)
        else:
            if (bbox.x0, bbox.y0) != (0, 0):
                raise errors.MalformedHocr("page's bounding box should start with (0, 0)")
            settings.page_size = bbox.x1, bbox.y1

    has_string = has_nonempty_string = False
    has_zone = has_char_zone = has_nonchar_zone = False
    children = get_children(node)
    if len(children) == 0:
        if djvu_class is const.TEXT_ZONE_PAGE:
            # For all other zone types, 0-child zone are simply skipped.
            # We return from the function here, to make further checks simpler.
            return [text_zones.Zone(type=djvu_class, bbox=bbox)]
        else:
            return []

    for child in children:
        if isinstance(child, basestring):
            has_string = True
            if child and not child.isspace():
                has_nonempty_string = True
        elif isinstance(child, text_zones.Zone):
            has_zone = True
            if child.type == const.TEXT_ZONE_CHARACTER:
                has_char_zone = True
            else:
                has_nonchar_zone = True
        else:
            raise TypeError('Unexpected %s object; expected a string or a text zone' % type(child).__name__)

    if has_zone:
        # Catch obvious inconsistencies early.
        if has_nonempty_string:
            raise errors.MalformedHocr("plain text intermixed with structural elements")
        if has_char_zone and has_nonchar_zone:
            raise errors.MalformedHocr("character zones intermixed with non-character zones")
        for child in children:
            if isinstance(child, text_zones.Zone):
                bbox.update(child.bbox)
        if len(children) == 0:
            return []

    if djvu_class <= const.TEXT_ZONE_WORD:
        if has_zone:
            return children
        elif has_string:
            if not bbox:
                raise errors.MalformedHocr("zone without bounding box information")
            text = ''.join(children)
            result = text_zones.Zone(type=const.TEXT_ZONE_CHARACTER, bbox=bbox, children=[text])
            # We return TEXT_ZONE_CHARACTER even it was a word according to hOCR.
            # Words need to be regrouped anyway.
            return [result]
        else:
            # Should not happen.
            assert 0

    if not has_zone:
        assert has_string
        if settings.cuneiform and settings.cuneiform == (0, 9):
            # hOCR produced by Cuneiform ≥ 0.9 is really awkward, let's work
            # around this.
            bboxes_node = node.find('span[@class="ocr_cinfo"]')
            if bboxes_node is not None and len(bboxes_node) == 0 and bboxes_node.text is None:
                title = bboxes_node.get('title') or ''
        text = ''.join(children)
        children = _apply_bboxes(djvu_class, title, text, settings)
        if len(children) == 0:
            return []
        if isinstance(children[0], basestring):
            # Get rid of e.g. trailing newlines.
            children[0] = children[0].rstrip()
            has_zone = has_nonchar_zone = has_char_zone = False
            has_string = True
        else:
            assert all(
                isinstance(child, text_zones.Zone) and
                child.type == const.TEXT_ZONE_WORD
                for child in children
            )
            has_zone = has_nonchar_zone = True
            has_string = has_char_zone = False

    if has_char_zone:
        break_iterator = functools.partial(unicode_support.word_break_iterator, locale=settings.uax29)
        children = text_zones.group_words(children, settings.details, break_iterator)
        has_string = False
        if len(children) == 0:
            return []

    if has_zone and has_string:
        assert not has_nonempty_string
        children = [child for child in children if not isinstance(child, basestring)]
        if len(children) == 0:
            return []

    assert len(children) > 0

    if not bbox:
        if len(node) == 0:
            # Ocropus 0.2 doesn't always provide necessary bounding box
            # information. We have no other choice than to drop such a broken
            # zone silently.
            # FIXME: This work-around is ugly and should be dropped at some point.
            return []
        if len(children) == 1:
            [child] = children
            if isinstance(child, basestring) and (child == '' or child.isspace()):
                return []
        raise errors.MalformedHocr("text zone without bounding box information")

    if settings.page_size is None:
        # At this point page size should be already known.
        raise errors.MalformedHocr('unable to determine page size')

    return [text_zones.Zone(type=djvu_class, bbox=bbox, children=children)]

def scan(node, settings):
    result = []
    for zone in _scan(node, settings):
        if isinstance(zone, basestring):
            if zone == '' or zone.isspace():
                continue
            else:
                raise errors.MalformedHocr("plain text intermixed with structural elements")
        if not isinstance(zone, text_zones.Zone):
            raise TypeError('Unexpected %s object; expected a text zone' % type(zone).__name__)
        result += [zone]
        zone.rotate(settings.rotation)
    return result

class ExtractSettings(object):

    def __init__(self, rotation=0, details=TEXT_DETAILS_WORD, uax29=None, page_size=None, cuneiform=None):
        self.rotation = rotation
        self.details = details
        if uax29 is not None:
            icu = unicode_support.get_icu()
            if uax29 is True:
                uax29 = icu.Locale()
            else:
                uax29 = icu.Locale(uax29)
        self.uax29 = uax29
        self.page_size = page_size
        self.cuneiform = cuneiform

def extract_text(stream, **kwargs):
    '''
    Extract DjVu text from an hOCR stream.

    details: TEXT_DETAILS_LINES or TEXT_DETAILS_WORD or TEXT_DETAILS_CHAR
    uax29: None or a PyICU locale
    '''
    settings = ExtractSettings(**kwargs)
    doc = etree.parse(stream, etree.HTMLParser())
    if doc.find('/head/meta[@name="ocr-capabilities"]') is None:
        ocr_system = doc.find('/head/meta[@name="ocr-system"]')
        if ocr_system is not None and ocr_system.get('content') == 'openocr':
            settings.cuneiform = (0, 9)
        elif ocr_system is None:
            # This is wild guess. However, since ocr-system is a required meta
            # tag, the hOCR we are processing is broken anyway.
            settings.cuneiform = (0, 8)
    scan_result = scan(doc.find('/body'), settings)
    return [zone.sexpr for zone in scan_result]

__all__ = [
    'extract_text',
    'TEXT_DETAILS_LINE', 'TEXT_DETAILS_WORD', 'TEXT_DETAILS_CHARACTER'
]

# vim:ts=4 sw=4 et
