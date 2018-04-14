#!/usr/bin/env python
# coding=utf-8

"""
  _  _  _    _  _    __,   _  _  _    _   ,_
 / |/ |/ |  / |/ |  /  |  / |/ |/ |  |/  /  |
   |  |  |_/  |  |_/\_/|_/  |  |  |_/|__/   |_/

mnamer (Media reNAMER) is an intelligent and highly configurable media
organization utility. It parses media filenames for metadata, searches the web
to fill in the blanks, and then renames and moves them.

See https://github.com/jkwill87/mnamer for more information.
"""

import json
from argparse import ArgumentParser
from builtins import input
from os import environ
from pathlib import Path
from re import sub, match
from shutil import move as shutil_move
from string import Template
from unicodedata import normalize

from appdirs import user_config_dir
from guessit import guessit
from mapi.exceptions import MapiNotFoundException
from mapi.metadata import Metadata, MetadataMovie, MetadataTelevision
from mapi.providers import provider_factory
from os.path import normpath, exists, expanduser
from termcolor import cprint

from mnamer import *


def notify(text):
    if IS_WINDOWS:
        cprint(text, color='yellow')
    else:
        cprint(text, attrs=['dark'])


def get_parameters():
    """ Retrieves program arguments from CLI parameters
    """

    help_usage = 'mnamer target [targets ...] [options] [directives]'

    help_options = '''
OPTIONS:
    mnamer attempts to load options from mnamer.json in the user's configuration
    directory, .mnamer.json in the current working directory, and then from the
    command line-- overriding each other also in that order.

    -b, --batch: batch mode; disables interactive prompts
    -s, --scene: scene mode; use dots in place of whitespace and non-ascii chars
    -r, --recurse: show this help message and exit
    -v, --verbose: increases output verbosity
    --blacklist <word,...>: ignores files matching these regular expressions
    --max_hits <number>: limits the maximum number of hits for each query
    --extension_mask <ext,...>: define extension mask used by the file parser
    --movie_api {tmdb}: set movie api provider
    --movie_destination <path>: set movie relocation destination
    --movie_template <template>: set movie renaming template
    --television_api {tvdb}: set television api provider
    --television_destination <path>: set television relocation destination
    --television_template <template>: set television renaming template'''

    help_directives = '''
DIRECTIVES:
    Whereas options configure how mnamer works, directives are one-off
    parameters that are used to perform secondary tasks like exporting the
    current option set to a file.

    --help: print this message and exit
    --test_run: mocks the renaming and moving of files
    --config_load < path >: import configuration from file
    --config_save < path >: save configuration to file
    --id < id >: explicitly specify movie or series id
    --media { movie, television }: override media detection
    --version: display mnamer version information and quit
    '''

    directive_keys = {
        'id',
        'media',
        'config_save',
        'config_load',
        'test_run',
        'version'
    }

    parser = ArgumentParser(
        prog='mnamer', add_help=False,
        epilog='visit https://github.com/jkwill87/mnamer for more info',
        usage=help_usage
    )

    # Target Parameters
    parser.add_argument('targets', nargs='*', default=[])

    # Configuration Parameters
    parser.add_argument('-b', '--batch', action='store_true', default=None)
    parser.add_argument('-s', '--scene', action='store_true', default=None)
    parser.add_argument('-r', '--recurse', action='store_true', default=None)
    parser.add_argument('-v', '--verbose', action='store_true', default=None)
    parser.add_argument('--blacklist', nargs='+', default=None)
    parser.add_argument('--max_hits', type=int, default=None)
    parser.add_argument('--extension_mask', nargs='+', default=None)
    parser.add_argument('--movie_api', choices=['tmdb'], default=None)
    parser.add_argument('--movie_destination', default=None)
    parser.add_argument('--movie_template', default=None)
    parser.add_argument('--television_api', choices=['tvdb'], default=None)
    parser.add_argument('--television_destination', default=None)
    parser.add_argument('--television_template', default=None)

    # Directive Parameters
    parser.add_argument('--help', action='store_true')
    parser.add_argument('--id')
    parser.add_argument('--media', choices=['movie', 'television'])
    parser.add_argument('--config_save', default=None)
    parser.add_argument('--config_load', default=None)
    parser.add_argument('--test_run', action='store_true')
    parser.add_argument('--version', action='store_true')

    arguments = vars(parser.parse_args())
    targets = arguments.pop('targets')
    directives = {key: arguments.pop(key, None) for key in directive_keys}
    config = {k: v for k, v in arguments.items() if v is not None}

    # Exit early if user ask for usage help
    if arguments['help'] is True:
        print(
            '\nUSAGE:\n    %s\n%s\n%s' %
            (help_usage, help_options, help_directives)
        )
        exit(0)
    return targets, config, directives


def config_load(path):
    """ Reads JSON file and overlays parsed values over current configs
    :param str path: the path of the config file to load from
    :return: key-value option pairs as loaded from file
    :rtype: dict
    """
    templated_path = Template(path).substitute(environ)
    with open(templated_path, mode='r') as file_pointer:
        data = json.load(file_pointer)
    return {k: v for k, v in data.items() if v is not None}


def config_save(path, config):
    """ Serializes Config object as a JSON file
    :param str path: the path of the config file to save to
    :param dict config: key-value options pairs to serialize
    """
    templated_path = Template(path).substitute(environ)
    with open(templated_path, mode='w') as file_pointer:
        json.dump(config, file_pointer, indent=4)


def dir_crawl(targets, recurse=False, ext_mask=None):
    """ Crawls a directory, searching for files
    :param bool recurse: will iterate through nested directories if true
    :param optional list ext_mask: only matches files with provided extensions
        if set
    :param str or list targets: paths (file or directory) to crawl through
    :rtype: list of Path
    """
    if not isinstance(targets, (list, tuple)):
        targets = [targets]
    files = []
    for target in targets:
        path = Path(target)
        if not path.exists():
            continue
        for found_file in dir_iter(path, recurse=recurse):
            if ext_mask and found_file.suffix.strip('.') not in ext_mask:
                continue
            files.append(found_file.resolve())
    seen = set()
    seen_add = seen.add
    return [Path(f).absolute() for f in files if not (f in seen or seen_add(f))]


def dir_iter(path, recurse=False, depth=0):
    """ Iterates through a directory, yielding each file found
    :param Path path: directory path to iterate through
    :param bool recurse: will iterate through nested directories if true
    :param int depth: recursion count
    """
    if path.is_file():
        yield path
    elif path.is_dir():
        for child in path.iterdir():
            if child.is_file():
                yield child
            elif not recurse:
                continue
            elif child.is_symlink() and depth != 0:
                continue
            for d in dir_iter(child, True, depth + 1):
                yield d


def provider_search(metadata, **options):
    """ An adapter for mapi's Provider classes
    :param Metadata metadata: metadata to use as the basis of search criteria
    :param dict options:
    :rtype: Metadata (yields)
    """
    media = metadata['media']
    if not hasattr(provider_search, "providers"):
        provider_search.providers = {}
    if media not in provider_search.providers:
        api = {
            'television': options.get('television_api'),
            'movie': options.get('movie_api')
        }.get(media)
        keys = {
            'tmdb': options.get('api_key_tmdb'),
            'tvdb': options.get('api_key_tvdb'),
            'imdb': None
        }
        provider_search.providers[media] = provider_factory(
            api, api_key=keys.get(api)
        )
    for result in provider_search.providers[media].search(**metadata):
        yield result


def meta_parse(path, media=None):
    """ Uses guessit to parse metadata from a filename
    :param Path path: the path to the file to parse
    :param optional Media media: overrides media detection
    :rtype: Metadata
    """
    abs_path_str = str(path.resolve())
    media = {
        'television': 'episode',
        'tv': 'episode',
        'movie': 'movie'
    }.get(media)
    data = dict(guessit(abs_path_str, {'type': media}))

    # Parse movie metadata
    if data.get('type') == 'movie':
        meta = MetadataMovie()
        if 'title' in data:
            meta['title'] = data['title']
        if 'year' in data:
            meta['date'] = '%s-01-01' % data['year']
        meta['media'] = 'movie'

    # Parse television metadata
    elif data.get('type') == 'episode':
        meta = MetadataTelevision()
        if 'title' in data:
            meta['series'] = data['title']
        if 'season' in data:
            meta['season'] = str(data['season'])
        if 'date' in data:
            meta['date'] = str(data['date'])
        if 'episode' in data:
            if isinstance(data['episode'], (list, tuple)):
                meta['episode'] = str(sorted(data['episode'])[0])
            else:
                meta['episode'] = str(data['episode'])
    else:
        raise ValueError('Could not determine media type')

    # Parse non-media specific fields
    quality_fields = [
        field for field in data if field in [
            'audio_profile',
            'screen_size',
            'video_codec',
            'video_profile'
        ]
    ]
    for field in quality_fields:
        if 'quality' not in meta:
            meta['quality'] = data[field]
        else:
            meta['quality'] += ' ' + data[field]
    if 'release_group' in data:
        meta['group'] = data['release_group']
    if path.suffix:
        meta['extension'] = path.suffix
    return meta


def merge_dicts(d1, d2):
    """ Merges two dictionaries
    :param d1: Base dictionary
    :param d2: Overlaying dictionary
    :rtype: dict
    """
    d3 = d1.copy()
    d3.update(d2)
    return d3


def sanitize_filename(filename, scene_mode=False, replacements=None):
    """ Removes illegal filename characters and condenses whitespace
    :param str filename: the filename to sanitize
    :param bool scene_mode: replace non-ascii and whitespace characters with
    dots if true
    :param optional dict replacements: words to replace prior to processing
    :rtype: str
    """
    for replacement in replacements:
        filename = filename.replace(replacement, replacements[replacement])
    if scene_mode is True:
        filename = normalize('NFKD', filename)
        filename.encode('ascii', 'ignore')
        filename = sub(r'\s+', '.', filename)
        filename = sub(r'[^.\d\w/]', '', filename)
        filename = filename.lower()
    else:
        filename = sub(r'\s+', ' ', filename)
        filename = sub(r'[^ \d\w?!.,_()\[\]\-/]', '', filename)
    return filename.strip()


def process_files(targets, media=None, test_run=False, **config):
    """ Processes targets, relocating them as needed

    :param list of str targets: files to process
    :param str media: overrides automatic media detection if set
    :param bool test_run: mocks relocation operation if True
    :param dict config: optional configuration kwargs
    :return:
    """
    # Begin processing files
    detection_count = 0
    success_count = 0
    for file_path in dir_crawl(
            targets,
            config.get('recurse', False),
            config.get('extension_mask')
    ):
        cprint('\nDetected File', attrs=['bold'])

        blacklist = config.get('blacklist', ())
        if any(match(b, file_path.stem.lower()) for b in blacklist):
            cprint('%s (blacklisted)' % file_path, attrs=['dark'])
            continue
        else:
            print(file_path.name)

        # Print metadata fields
        meta = meta_parse(file_path, media)
        if config['verbose'] is True:
            for field, value in meta.items():
                print('  - %s: %s' % (field, value))

        # Print search results
        detection_count += 1
        cprint('\nQuery Results', attrs=['bold'])
        results = provider_search(meta, **config)
        i = 1
        hits = []
        max_hits = int(config.get('max_hits', 15))
        while i < max_hits:
            try:
                hit = next(results)
                print("  [%s] %s" % (i, hit))
                hits.append(hit)
                i += 1
            except (StopIteration, MapiNotFoundException):
                break

        # Skip hit if no hits
        if not hits:
            notify('  - None found! Skipping.')
            continue

        # Select first if batch
        if config.get('batch') is True:
            meta.update(hits[0])

        # Prompt user for input
        else:
            print('  [RETURN] for default, [s]kip, [q]uit')
            abort = skip = None
            while True:
                selection = input('  > Your Choice? ')

                # Catch default selection
                if not selection:
                    meta.update(hits[0])
                    break

                # Catch skip hit (just break w/o changes)
                elif selection in ['s', 'S', 'skip', 'SKIP']:
                    skip = True
                    break

                # Quit (abort and exit)
                elif selection in ['q', 'Q', 'quit', 'QUIT']:
                    abort = True
                    break

                # Catch result choice within presented range
                elif selection.isdigit() and 0 < int(selection) < len(
                        hits) + 1:
                    meta.update(hits[int(selection) - 1])
                    break

                # Re-prompt if user input is invalid wrt to presented options
                else:
                    print('\nInvalid selection, please try again.')

            # User requested to skip file...
            if skip is True:
                notify('  - Skipping rename, as per user request.')
                continue

            # User requested to exit...
            elif abort is True:
                notify('\nAborting, as per user request.')
                return

        # Create file path
        cprint('\nProcessing File', attrs=['bold'])
        media = meta['media']
        template = config.get('%s_template' % media)
        dest_path = meta.format(template)
        if config.get('%s_destination' % media):
            dest_dir = meta.format(config.get('%s_destination' % media, ''))
            dest_path = '%s/%s' % (dest_dir, dest_path)
        dest_path = sanitize_filename(
            dest_path,
            config.get('scene', False),
            config.get('replacements')
        )
        dest_path = Path(dest_path)

        # Attempt to process file
        try:
            if not test_run:
                if exists(str(dest_path.parent)) is False:
                    dest_path.parent.mkdir(parents=True)
                shutil_move(str(file_path), str(dest_path))
            print("  - Relocating file to '%s'" % dest_path)
        except IOError:
            cprint('  - Failed!', 'red')
        else:
            cprint('  - Success!', 'green')
            success_count += 1

    # Summarize session outcome
    if not detection_count:
        notify('\nNo media files found. "mnamer --help" for usage.')
        return

    if success_count == 0:
        outcome_colour = 'red'
    elif success_count < detection_count:
        outcome_colour = 'yellow'
    else:
        outcome_colour = 'green'
    cprint(
        '\nSuccessfully processed %s out of %s files' %
        (success_count, detection_count),
        outcome_colour
    )


def main():
    """ Program entry point
    """

    # Process parameters
    targets, config, directives = get_parameters()

    # Display version information and exit if requested
    if directives.get('version') is True:
        print('mnamer v%s' % VERSION)
        return

    # Detect file(s)
    cprint('Starting mnamer', attrs=['bold'])
    for file_path in [
        '.mnamer.json',
        normpath('%s/mnamer.json' % user_config_dir()),
        normpath('%s/.mnamer.json' % expanduser('~')),
        directives['config_load']
    ]:
        if not file_path:
            continue
        try:
            config = merge_dicts(config_load(file_path), config)
            cprint('  - success loading config from %s' % file_path,
                   color='green')
        except (TypeError, IOError):
            if config.get('verbose'):
                notify('  - skipped loading config from %s' % file_path)

    # Backfill configuration with defaults
    config = merge_dicts(CONFIG_DEFAULTS, config)

    # Save config to file if requested
    if directives.get('config_save'):
        file_path = directives['config_save']
        try:
            config_save(file_path, config)
            print('success saving to %s' % directives['config_save'])
        except (TypeError, IOError):
            if config.get('verbose') is True:
                print('error saving config to %s' % file_path)

    # Display config information
    if config.get('verbose') is True:
        cprint('\nConfiguration', attrs=['bold'])
        for key, value in config.items():
            print("  - %s: %s" % (key, None if value == '' else value))

    # Process Files
    media = directives.get('media')
    test_run = directives.get('test_run')
    process_files(targets, media, test_run, **config)


if __name__ == '__main__':
    main()
