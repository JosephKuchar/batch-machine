from __future__ import division
import logging; _L = logging.getLogger('openaddr.preview')

from zipfile import ZipFile
from io import TextIOWrapper
from csv import DictReader
from tempfile import mkstemp
from math import pow, sqrt, pi, log
from argparse import ArgumentParser
from urllib.parse import urlparse
import json, itertools, os

import requests, uritemplate

from osgeo import osr, ogr
from .compat import cairo

TILE_URL = 'http://tile.mapzen.com/mapzen/vector/v1/all/{z}/{x}/{y}.json{?api_key}'
EARTH_DIAMETER = 6378137 * 2 * pi

# WGS 84, http://spatialreference.org/ref/epsg/4326/
EPSG4326 = '+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs'

# Web Mercator, https://trac.osgeo.org/openlayers/wiki/SphericalMercator
EPSG900913 = '+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 +x_0=0.0 +y_0=0 +k=1.0 +units=m +nadgrids=@null +no_defs'

def render(filename_or_url, png_filename, width, resolution, mapzen_key):
    '''
    '''
    src_filename = get_local_filename(filename_or_url)
    points = project_points(iterate_file_points(src_filename))
    xmin, ymin, xmax, ymax = calculate_bounds(points)
    
    surface, context, scale = make_context(xmin, ymin, xmax, ymax, width, resolution)

    _L.info('Preview width {:.0f}, scale {:.5f}, zoom {:.2f}'.format(width, scale, calculate_zoom(scale, resolution)))

    # Map units per reference pixel (http://www.w3.org/TR/css3-values/#reference-pixel)
    muppx = resolution / scale
    
    black = 0x00, 0x00, 0x00
    off_white = 0xFF/0xFF, 0xFC/0xFF, 0xF9/0xFF
    point_fill = 0x74/0xFF, 0xA5/0xFF, 0x78/0xFF
    water_fill = 0xC7/0xFF, 0xDE/0xFF, 0xF5/0xFF # 0xDD/0xFF, 0xEA/0xFF, 0xF8/0xFF
    road_stroke = 0xC0/0xFF, 0xE0/0xFF, 0xE0/0xFF # 0xE0/0xFF, 0xE3/0xFF, 0xE5/0xFF
    park_fill = 0xDD/0xFF, 0xF6/0xFF, 0xDE/0xFF
    orange_over_land = 0xFE/0xFF, 0xCB/0xFF, 0x9F/0xFF
    orange_over_water = 0xE2/0xFF, 0xBB/0xFF, 0x9A/0xFF
    orange_darker = 0xFE/0xFF, 0x96/0xFF, 0x3F/0xFF, 0.5

    context.set_source_rgb(*off_white)
    context.rectangle(xmin, ymax, xmax - xmin, ymin - ymax)
    context.fill()
    
    landuse_geoms, water_geoms, roads_geoms = \
        get_map_features(xmin, ymin, xmax, ymax, resolution, scale, mapzen_key)
    
    fill_geometries(context, landuse_geoms, muppx, park_fill)
    fill_geometries(context, water_geoms, muppx, water_fill)

    context.set_line_width(.5 * muppx)
    context.set_source_rgb(*road_stroke)
    stroke_geometries(context, roads_geoms)
    
    context.set_line_width(.25 * muppx)

    for (x, y) in points:
        context.arc(x, y, 15, 0, 2 * pi)
        context.set_source_rgb(*point_fill)
        context.fill()
        context.arc(x, y, 15, 0, 2 * pi)
        context.set_source_rgb(*black)
        context.stroke()
    
    surface.write_to_png(png_filename)

def get_local_filename(filename_or_url):
    '''
    '''
    parsed = urlparse(filename_or_url)
    suffix = os.path.splitext(parsed.path)[1]
    
    if parsed.scheme in ('', 'file'):
        return filename_or_url
    
    if parsed.scheme not in ('http', 'https'):
        raise ValueError('Unknown URL type: {}'.format(filename_or_url))
    
    _L.info('Downloading {}...'.format(filename_or_url))

    got = requests.get(filename_or_url)
    _, filename = mkstemp(prefix='Preview-', suffix=suffix)

    with open(filename, 'wb') as file:
        file.write(got.content)
        _L.debug('Saved to {}'.format(filename))
    
    return filename

def iterate_file_points(filename):
    '''
    '''
    suffix = os.path.splitext(filename)[1].lower()
    
    if suffix == '.csv':
        open_file = open(filename, 'r')
    elif suffix == '.zip':
        open_file = open(filename, 'rb')
    
    with open_file as file:
        if suffix == '.csv':
            csv_file = file
        elif suffix == '.zip':
            zip = ZipFile(file)
            csv_names = [name for name in zip.namelist() if name.endswith('.csv')]
            csv_file = TextIOWrapper(zip.open(csv_names[0]))
        
        for row in DictReader(csv_file):
            try:
                lon, lat = float(row['LON']), float(row['LAT'])
            except:
                continue
            
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                yield (lon, lat)

def get_map_features(xmin, ymin, xmax, ymax, resolution, scale, mapzen_key):
    '''
    '''
    zoom = round(calculate_zoom(scale, resolution))
    mincol = 2**zoom * (xmin + EARTH_DIAMETER/2) / EARTH_DIAMETER
    minrow = 2**zoom * (EARTH_DIAMETER/2 - ymax) / EARTH_DIAMETER
    maxcol = 2**zoom * (xmax + EARTH_DIAMETER/2) / EARTH_DIAMETER
    maxrow = 2**zoom * (EARTH_DIAMETER/2 - ymin) / EARTH_DIAMETER
    
    row_cols = itertools.product(range(int(minrow), int(maxrow) + 1),
                                 range(int(mincol), int(maxcol) + 1))

    landuse_geoms, water_geoms, roads_geoms = list(), list(), list()
    project = get_projection()
    
    def projected_geom(feature):
        geom = ogr.CreateGeometryFromJson(json.dumps(feature['geometry']))
        geom.Transform(project)
        return geom
    
    for (row, col) in row_cols:
        url = uritemplate.expand(TILE_URL, dict(z=zoom, x=col, y=row, api_key=mapzen_key))
        got = requests.get(url)

        for feature in got.json()['landuse']['features']:
            if 'Polygon' in feature['geometry']['type']:
                if feature['properties'].get('kind') in ('cemetery', 'forest', 'golf_course', 'grave_yard', 'meadow', 'park', 'pitch', 'wood'):
                    landuse_geoms.append(projected_geom(feature))

        for feature in got.json()['water']['features']:
            if 'Polygon' in feature['geometry']['type']:
                if feature['properties']['kind'] in ('basin', 'lake', 'ocean', 'riverbank', 'water'):
                    water_geoms.append(projected_geom(feature))

        for feature in got.json()['roads']['features']:
            if 'LineString' in feature['geometry']['type']:
                if feature['properties']['kind'] in ('highway', 'major_road', 'minor_road', 'rail', 'path'):
                    roads_geoms.append(projected_geom(feature))

        _L.debug('Getting tile {}'.format(url))
    
    return landuse_geoms, water_geoms, roads_geoms

def get_projection():
    '''
    '''
    osr.UseExceptions()
    sref_geo = osr.SpatialReference(); sref_geo.ImportFromProj4(EPSG4326)
    sref_map = osr.SpatialReference(); sref_map.ImportFromProj4(EPSG900913)
    return osr.CoordinateTransformation(sref_geo, sref_map)

def project_points(lonlats):
    '''
    '''
    project = get_projection()
    points = list()
    
    for (lon, lat) in lonlats:
        geom = ogr.CreateGeometryFromWkt('POINT({:.7f} {:.7f})'.format(lon, lat))
        geom.Transform(project)
        points.append((geom.GetX(), geom.GetY()))
    
    return points

def stats(values):
    '''
    '''
    mean = sum(values) / len(values)
    deviations = [pow(val - mean, 2) for val in values]
    stddev = sqrt(sum(deviations) / len(values))

    return mean, stddev

def calculate_zoom(scale, resolution):
    ''' Calculate web map zoom based on scale.
    '''
    scale_at_zero = resolution * 256 / EARTH_DIAMETER
    zoom = log(scale / scale_at_zero) / log(2)
    
    return zoom

def calculate_bounds(points):
    '''
    '''
    xs, ys = zip(*points)

    # use standard deviation to avoid far-flung mistakes, and look further
    # horizontally to account for Github comment thread image appearance.
    (xmean, xsdev), (ymean, ysdev) = stats(xs), stats(ys)
    xmin, xmax = xmean - 5 * xsdev, xmean + 5 * xsdev
    ymin, ymax = ymean - 3 * ysdev, ymean + 3 * ysdev
    
    # look at the actual points
    okay_xs = [x for (x, y) in points if (xmin <= x <= xmax)]
    okay_ys = [y for (x, y) in points if (ymin <= y <= ymax)]
    left, bottom = min(okay_xs), min(okay_ys)
    right, top = max(okay_xs), max(okay_ys)
    
    # pad by 2% on all sides
    width, height = right - left, top - bottom
    left -= width / 50
    bottom -= height / 50
    right += width / 50
    top += height / 50
    
    return left, bottom, right, top
    
def make_context(left, bottom, right, top, width=668, resolution=1):
    ''' Get Cairo surface, context, and drawing scale.
    
        668px is the width of a comment box in Github, one place where
        these previews are designed to be used.
    '''
    aspect = (right - left) / (top - bottom)

    hsize = int(resolution * width)
    vsize = int(hsize / aspect)

    hscale = hsize / (right - left)
    vscale = (hsize / aspect) / (bottom - top)

    hoffset = -left
    voffset = -top

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, hsize, vsize)
    context = cairo.Context(surface)
    context.scale(hscale, vscale)
    context.translate(hoffset, voffset)
    
    return surface, context, hscale

def stroke_geometries(ctx, geometries):
    '''
    '''
    for geometry in geometries:
        if geometry.GetGeometryType() in (ogr.wkbMultiPolygon, ogr.wkbMultiLineString):
            parts = geometry
        elif geometry.GetGeometryType() in (ogr.wkbPolygon, ogr.wkbLineString):
            parts = [geometry]
        else:
            continue

        for part in parts:
            if part.GetGeometryType() is ogr.wkbPolygon:
                rings = part
            else:
                rings = [part]

            for ring in rings:
                points = ring.GetPoints()
                if geometry.GetGeometryType() in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
                    draw_line(ctx, points[-1], points)
                else:
                    draw_line(ctx, points[0], points[1:])
                ctx.stroke()

def fill_geometries(ctx, geometries, muppx, rgb):
    '''
    '''
    ctx.set_source_rgb(*rgb)

    for geometry in geometries:
        if geometry.GetGeometryType() == ogr.wkbMultiPolygon:
            parts = geometry
        elif geometry.GetGeometryType() == ogr.wkbPolygon:
            parts = [geometry]
        elif geometry.GetGeometryType() == ogr.wkbPoint:
            buffer = geometry.Buffer(2 * muppx, 3)
            parts = [buffer]
        else:
            raise NotImplementedError()

        for part in parts:
            for ring in part:
                points = ring.GetPoints()
                draw_line(ctx, points[-1], points)
            ctx.fill()

def draw_line(ctx, start, points):
    '''
    '''
    ctx.move_to(*start)

    for point in points:
        ctx.line_to(*point)

parser = ArgumentParser(description='Draw a map of a single source preview.')

parser.add_argument('src_filename', help='Input Zip or CSV filename or URL.')
parser.add_argument('png_filename', help='Output PNG filename.')

parser.set_defaults(resolution=1, width=668)

parser.add_argument('--2x', dest='resolution', action='store_const', const=2,
                    help='Draw at double resolution.')

parser.add_argument('--1x', dest='resolution', action='store_const', const=1,
                    help='Draw at normal resolution.')

parser.add_argument('--width', dest='width', type=int,
                    help='Width in pixels.')

parser.add_argument('--mapzen-key', dest='mapzen_key',
                    help='Mapzen API Key. See: https://mapzen.com/documentation/overview/')

parser.add_argument('-v', '--verbose', help='Turn on verbose logging',
                    action='store_const', dest='loglevel',
                    const=logging.DEBUG, default=logging.INFO)

parser.add_argument('-q', '--quiet', help='Turn off most logging',
                    action='store_const', dest='loglevel',
                    const=logging.WARNING, default=logging.INFO)

def main():
    args = parser.parse_args()
    from .ci import setup_logger
    setup_logger(None, None, None, log_level=args.loglevel)
    render(args.src_filename, args.png_filename, args.width, args.resolution, args.mapzen_key)

if __name__ == '__main__':
    exit(main())