# -*- coding: utf-8 -*-
import base64
import os
import re
import hashlib
import itertools
import json
import textwrap
import uuid
try:
    import sass as libsass
except ImportError:
    # If the `sass` python library isn't found, we fallback on the
    # `sassc` executable in the path.
    libsass = None
from datetime import datetime
from subprocess import Popen, PIPE
from odoo import fields, tools
from odoo.http import request
from odoo.modules.module import get_resource_path
import psycopg2
from odoo.tools import func, misc

import logging
_logger = logging.getLogger(__name__)

MAX_CSS_RULES = 4095


class CompileError(RuntimeError): pass
def rjsmin(script):
    """ Minify js with a clever regex.
    Taken from http://opensource.perlig.de/rjsmin
    Apache License, Version 2.0 """
    def subber(match):
        """ Substitution callback """
        groups = match.groups()
        return (
            groups[0] or
            groups[1] or
            groups[2] or
            groups[3] or
            (groups[4] and '\n') or
            (groups[5] and ' ') or
            (groups[6] and ' ') or
            (groups[7] and ' ') or
            ''
        )

    result = re.sub(
        r'([^\047"/\000-\040]+)|((?:(?:\047[^\047\\\r\n]*(?:\\(?:[^\r\n]|\r?'
        r'\n|\r)[^\047\\\r\n]*)*\047)|(?:"[^"\\\r\n]*(?:\\(?:[^\r\n]|\r?\n|'
        r'\r)[^"\\\r\n]*)*"))[^\047"/\000-\040]*)|(?:(?<=[(,=:\[!&|?{};\r\n]'
        r')(?:[\000-\011\013\014\016-\040]|(?:/\*[^*]*\*+(?:[^/*][^*]*\*+)*/'
        r'))*((?:/(?![\r\n/*])[^/\\\[\r\n]*(?:(?:\\[^\r\n]|(?:\[[^\\\]\r\n]*'
        r'(?:\\[^\r\n][^\\\]\r\n]*)*\]))[^/\\\[\r\n]*)*/)[^\047"/\000-\040]*'
        r'))|(?:(?<=[\000-#%-,./:-@\[-^`{-~-]return)(?:[\000-\011\013\014\01'
        r'6-\040]|(?:/\*[^*]*\*+(?:[^/*][^*]*\*+)*/))*((?:/(?![\r\n/*])[^/'
        r'\\\[\r\n]*(?:(?:\\[^\r\n]|(?:\[[^\\\]\r\n]*(?:\\[^\r\n][^\\\]\r\n]'
        r'*)*\]))[^/\\\[\r\n]*)*/)[^\047"/\000-\040]*))|(?<=[^\000-!#%&(*,./'
        r':-@\[\\^`{|~])(?:[\000-\011\013\014\016-\040]|(?:/\*[^*]*\*+(?:[^/'
        r'*][^*]*\*+)*/))*(?:((?:(?://[^\r\n]*)?[\r\n]))(?:[\000-\011\013\01'
        r'4\016-\040]|(?:/\*[^*]*\*+(?:[^/*][^*]*\*+)*/))*)+(?=[^\000-\040"#'
        r'%-\047)*,./:-@\\-^`|-~])|(?<=[^\000-#%-,./:-@\[-^`{-~-])((?:[\000-'
        r'\011\013\014\016-\040]|(?:/\*[^*]*\*+(?:[^/*][^*]*\*+)*/)))+(?=[^'
        r'\000-#%-,./:-@\[-^`{-~-])|(?<=\+)((?:[\000-\011\013\014\016-\040]|'
        r'(?:/\*[^*]*\*+(?:[^/*][^*]*\*+)*/)))+(?=\+)|(?<=-)((?:[\000-\011\0'
        r'13\014\016-\040]|(?:/\*[^*]*\*+(?:[^/*][^*]*\*+)*/)))+(?=-)|(?:[\0'
        r'00-\011\013\014\016-\040]|(?:/\*[^*]*\*+(?:[^/*][^*]*\*+)*/))+|(?:'
        r'(?:(?://[^\r\n]*)?[\r\n])(?:[\000-\011\013\014\016-\040]|(?:/\*[^*'
        r']*\*+(?:[^/*][^*]*\*+)*/))*)+', subber, '\n%s\n' % script
    ).strip()
    return result


class AssetError(Exception):
    pass


class AssetNotFound(AssetError):
    pass


class AssetsBundle(object):
    rx_css_import = re.compile("(@import[^;{]+;?)", re.M)
    rx_preprocess_imports = re.compile("""(@import\s?['"]([^'"]+)['"](;?))""")
    rx_css_split = re.compile("\/\*\! ([a-f0-9-]+) \*\/")

    def __init__(self, name, files, remains, env=None):
        self.name = name
        self.env = request.env if env is None else env
        self.max_css_rules = self.env.context.get('max_css_rules', MAX_CSS_RULES)
        self.javascripts = []
        self.stylesheets = []
        self.css_errors = []
        self.remains = []
        self._checksum = None
        self.files = files
        self.remains = remains
        for f in files:
            if f['atype'] == 'text/sass':
                self.stylesheets.append(SassStylesheetAsset(self, url=f['url'], filename=f['filename'], inline=f['content'], media=f['media']))
            elif f['atype'] == 'text/scss':
                self.stylesheets.append(ScssStylesheetAsset(self, url=f['url'], filename=f['filename'], inline=f['content'], media=f['media']))
            elif f['atype'] == 'text/less':
                self.stylesheets.append(LessStylesheetAsset(self, url=f['url'], filename=f['filename'], inline=f['content'], media=f['media']))
            elif f['atype'] == 'text/css':
                self.stylesheets.append(StylesheetAsset(self, url=f['url'], filename=f['filename'], inline=f['content'], media=f['media']))
            elif f['atype'] == 'text/javascript':
                self.javascripts.append(JavascriptAsset(self, url=f['url'], filename=f['filename'], inline=f['content']))

    def to_html(self, sep=None, css=True, js=True, debug=False, async=False, url_for=(lambda url: url)):
        if sep is None:
            sep = u'\n            '
        response = []
        if debug == 'assets':
            if css and self.stylesheets:
                is_css_preprocessed, old_attachments = self.is_css_preprocessed()
                if not is_css_preprocessed:
                    self.preprocess_css(debug=debug, old_attachments=old_attachments)
                    if self.css_errors:
                        msg = '\n'.join(self.css_errors)
                        response.append(JavascriptAsset(self, inline=self.dialog_message(msg)).to_html())
                        response.append(StylesheetAsset(self, url="/web/static/lib/bootstrap/css/bootstrap.css").to_html())
                if not self.css_errors:
                    for style in self.stylesheets:
                        response.append(style.to_html())

            if js:
                for jscript in self.javascripts:
                    response.append(jscript.to_html())
        else:
            if css and self.stylesheets:
                css_attachments = self.css() or []
                for attachment in css_attachments:
                    response.append(u'<link href="%s" rel="stylesheet"/>' % url_for(attachment.url))
                if self.css_errors:
                    msg = '\n'.join(self.css_errors)
                    response.append(JavascriptAsset(self, inline=self.dialog_message(msg)).to_html())
            if js and self.javascripts:
                response.append(u'<script %s type="text/javascript" src="%s"></script>' % (async and u'async="async"' or '', url_for(self.js().url)))
        response.extend(self.remains)

        return sep + sep.join(response)

    @func.lazy_property
    def last_modified(self):
        """Returns last modified date of linked files"""
        return max(itertools.chain(
            (asset.last_modified for asset in self.javascripts),
            (asset.last_modified for asset in self.stylesheets),
        ))

    @func.lazy_property
    def version(self):
        return self.checksum[0:7]

    @func.lazy_property
    def checksum(self):
        """
        Not really a full checksum.
        We compute a SHA1 on the rendered bundle + max linked files last_modified date
        """
        check = u"%s%s%s" % (json.dumps(self.files, sort_keys=True), u",".join(self.remains), self.last_modified)
        return hashlib.sha1(check.encode('utf-8')).hexdigest()

    def clean_attachments(self, type):
        """ Takes care of deleting any outdated ir.attachment records associated to a bundle before
        saving a fresh one.

        When `type` is css we need to check that we are deleting a different version (and not *any*
        version) because css may be paginated and, therefore, may produce multiple attachments for
        the same bundle's version.

        When `type` is js we need to check that we are deleting a different version (and not *any*
        version) because, as one of the creates in `save_attachment` can trigger a rollback, the
        call to `clean_attachments ` is made at the end of the method in order to avoid the rollback
        of an ir.attachment unlink (because we cannot rollback a removal on the filestore), thus we
        must exclude the current bundle.
        """
        ira = self.env['ir.attachment']
        domain = [
            ('url', '=like', '/web/content/%-%/{0}%.{1}'.format(self.name, type)),  # The wilcards are id, version and pagination number (if any)
            '!', ('url', '=like', '/web/content/%-{}/%'.format(self.version))
        ]

        # force bundle invalidation on other workers
        self.env['ir.qweb'].clear_caches()

        return ira.sudo().search(domain).unlink()

    def get_attachments(self, type, ignore_version=False):
        """ Return the ir.attachment records for a given bundle. This method takes care of mitigating
        an issue happening when parallel transactions generate the same bundle: while the file is not
        duplicated on the filestore (as it is stored according to its hash), there are multiple
        ir.attachment records referencing the same version of a bundle. As we don't want to source
        multiple time the same bundle in our `to_html` function, we group our ir.attachment records
        by file name and only return the one with the max id for each group.
        """
        version = "%" if ignore_version else self.version
        url_pattern = '/web/content/%-{0}/{1}{2}.{3}'.format(version, self.name, '.%' if type == 'css' else '', type)
        self.env.cr.execute("""
             SELECT max(id)
               FROM ir_attachment
              WHERE url like %s
           GROUP BY datas_fname
           ORDER BY datas_fname
         """, [url_pattern])
        attachment_ids = [r[0] for r in self.env.cr.fetchall()]
        return self.env['ir.attachment'].sudo().browse(attachment_ids)

    def save_attachment(self, type, content, inc=None):
        assert type in ('js', 'css')
        ira = self.env['ir.attachment']

        fname = '%s%s.%s' % (self.name, ('' if inc is None else '.%s' % inc), type)
        mimetype = 'application/javascript' if type == 'js' else 'text/css'
        values = {
            'name': "/web/content/%s" % type,
            'datas_fname': fname,
            'mimetype' : mimetype,
            'res_model': 'ir.ui.view',
            'res_id': False,
            'type': 'binary',
            'public': True,
            'datas': base64.b64encode(content.encode('utf8')),
        }
        attachment = ira.sudo().create(values)

        url = '/web/content/%s-%s/%s' % (attachment.id, self.version, fname)
        values = {
            'name': url,
            'url': url,
        }
        attachment.write(values)

        if self.env.context.get('commit_assetsbundle') is True:
            self.env.cr.commit()

        self.clean_attachments(type)

        return attachment

    def js(self):
        attachments = self.get_attachments('js')
        if not attachments:
            content = ';\n'.join(asset.minify() for asset in self.javascripts)
            return self.save_attachment('js', content)
        return attachments[0]

    def css(self):
        attachments = self.get_attachments('css')
        if not attachments:
            # get css content
            css = self.preprocess_css()
            if self.css_errors:
                return self.get_attachments('css', ignore_version=True)

            # move up all @import rules to the top
            matches = []
            css = re.sub(self.rx_css_import, lambda matchobj: matches.append(matchobj.group(0)) and '', css)
            matches.append(css)
            css = u'\n'.join(matches)

            # split for browser max file size and browser max expression
            re_rules = '([^{]+\{(?:[^{}]|\{[^{}]*\})*\})'
            re_selectors = '()(?:\s*@media\s*[^{]*\{)?(?:\s*(?:[^,{]*(?:,|\{(?:[^}]*\}))))'
            page = []
            pages = [page]
            page_selectors = 0
            for rule in re.findall(re_rules, css):
                selectors = len(re.findall(re_selectors, rule))
                if page_selectors + selectors <= self.max_css_rules:
                    page_selectors += selectors
                    page.append(rule)
                else:
                    pages.append([rule])
                    page = pages[-1]
                    page_selectors = selectors
            for idx, page in enumerate(pages):
                self.save_attachment("css", ' '.join(page), inc=idx)
            attachments = self.get_attachments('css')
        return attachments

    def dialog_message(self, message):
        return """
            (function (message) {
                if (window.__assetsBundleErrorSeen) return;
                window.__assetsBundleErrorSeen = true;

                document.addEventListener("DOMContentLoaded", function () {
                    var alertTimeout = setTimeout(alert.bind(window, message), 0);
                    if (typeof odoo === "undefined") return;

                    odoo.define("AssetsBundle.ErrorMessage", function (require) {
                        "use strict";

                        var base = require("web_editor.base");
                        var core = require("web.core");
                        var Dialog = require("web.Dialog");

                        var _t = core._t;

                        clearTimeout(alertTimeout);

                        base.ready().then(function () {
                            new Dialog(null, {
                                title: _t("Style error"),
                                $content: $("<div/>")
                                    .append($("<p/>", {text: _t("The style compilation failed, see the error below. Your recent actions may be the cause, please try reverting the changes you made.")}))
                                    .append($("<pre/>", {html: message})),
                            }).open();
                        });
                    });
                });
            })("%s");
        """ % message.replace('"', '\\"').replace('\n', '&NewLine;')

    def is_css_preprocessed(self):
        preprocessed = True
        attachments = None
        for atype in (SassStylesheetAsset, ScssStylesheetAsset, LessStylesheetAsset):
            outdated = False
            assets = dict((asset.html_url, asset) for asset in self.stylesheets if isinstance(asset, atype))
            if assets:
                assets_domain = [('url', 'in', list(assets))]
                attachments = self.env['ir.attachment'].sudo().search(assets_domain)
                for attachment in attachments:
                    asset = assets[attachment.url]
                    if asset.last_modified > fields.Datetime.from_string(attachment['__last_update']):
                        outdated = True
                        break
                    if asset._content is None:
                        asset._content = attachment.datas and base64.b64decode(attachment.datas).decode('utf8') or ''
                        if not asset._content and attachment.file_size > 0:
                            asset._content = None # file missing, force recompile

                if any(asset._content is None for asset in assets.values()):
                    outdated = True

                if outdated:
                    preprocessed = False

        return preprocessed, attachments

    def preprocess_css(self, debug=False, old_attachments=None):
        """
            Checks if the bundle contains any sass/less content, then compiles it to css.
            Returns the bundle's flat css.
        """
        for atype in (SassStylesheetAsset, ScssStylesheetAsset, LessStylesheetAsset):
            assets = [asset for asset in self.stylesheets if isinstance(asset, atype)]
            if assets:
                source = '\n'.join([asset.get_source() for asset in assets])
                compiled = self.compile_css(assets[0].compile, source)

                if not self.css_errors and old_attachments:
                    old_attachments.unlink()

                fragments = self.rx_css_split.split(compiled)
                at_rules = fragments.pop(0)
                if at_rules:
                    # Sass and less moves @at-rules to the top in order to stay css 2.1 compatible
                    self.stylesheets.insert(0, StylesheetAsset(self, inline=at_rules))
                while fragments:
                    asset_id = fragments.pop(0)
                    asset = next(asset for asset in self.stylesheets if asset.id == asset_id)
                    asset._content = fragments.pop(0)

                    if debug:
                        try:
                            fname = os.path.basename(asset.url)
                            url = asset.html_url
                            with self.env.cr.savepoint():
                                self.env['ir.attachment'].sudo().create(dict(
                                    datas=base64.b64encode(asset.content.encode('utf8')),
                                    mimetype='text/css',
                                    type='binary',
                                    name=url,
                                    url=url,
                                    datas_fname=fname,
                                    res_model=False,
                                    res_id=False,
                                ))

                            if self.env.context.get('commit_assetsbundle') is True:
                                self.env.cr.commit()
                        except psycopg2.Error:
                            pass

        return '\n'.join(asset.minify() for asset in self.stylesheets)

    def compile_css(self, compiler, source):
        """Sanitizes @import rules, remove duplicates @import rules, then compile"""
        imports = []
        def handle_compile_error(e, source):
            error = self.get_preprocessor_error(e, source=source)
            _logger.warning(error)
            self.css_errors.append(error)
            return ''
        def sanitize(matchobj):
            ref = matchobj.group(2)
            line = '@import "%s"%s' % (ref, matchobj.group(3))
            if '.' not in ref and line not in imports and not ref.startswith(('.', '/', '~')):
                imports.append(line)
                return line
            msg = "Local import '%s' is forbidden for security reasons. Please remove all @import {your_file} imports in your custom files. In Odoo you have to import all files in the assets, and not through the @import statement." % ref
            _logger.warning(msg)
            self.css_errors.append(msg)
            return ''
        source = re.sub(self.rx_preprocess_imports, sanitize, source)

        try:
            compiled = compiler(source)
            return compiled.strip()
        except CompileError as e:
            return handle_compile_error(e, source=source)

    def get_preprocessor_error(self, stderr, source=None):
        """Improve and remove sensitive information from sass/less compilator error messages"""
        error = misc.ustr(stderr).split('Load paths')[0].replace('  Use --trace for backtrace.', '')
        if 'Cannot load compass' in error:
            error += "Maybe you should install the compass gem using this extra argument:\n\n" \
                     "    $ sudo gem install compass --pre\n"
        error += "This error occured while compiling the bundle '%s' containing:" % self.name
        for asset in self.stylesheets:
            if isinstance(asset, PreprocessedCSS):
                error += '\n    - %s' % (asset.url if asset.url else '<inline sass>')
        return error


class WebAsset(object):
    html_url_format = '%s'
    _content = None
    _filename = None
    _ir_attach = None
    _id = None

    def __init__(self, bundle, inline=None, url=None, filename=None):
        self.bundle = bundle
        self.inline = inline
        self._filename = filename
        self.url = url
        self.html_url_args = url
        if not inline and not url:
            raise Exception("An asset should either be inlined or url linked, defined in bundle '%s'" % bundle.name)

    @func.lazy_property
    def id(self):
        if self._id is None: self._id = str(uuid.uuid4())
        return self._id

    @func.lazy_property
    def name(self):
        name = '<inline asset>' if self.inline else self.url
        return "%s defined in bundle '%s'" % (name, self.bundle.name)

    @property
    def html_url(self):
        return self.html_url_format % self.html_url_args

    def stat(self):
        if not (self.inline or self._filename or self._ir_attach):
            path = (segment for segment in self.url.split('/') if segment)
            self._filename = get_resource_path(*path)
            if self._filename:
                return
            try:
                # Test url against ir.attachments
                fields = ['__last_update', 'datas', 'mimetype']
                domain = [('type', '=', 'binary'), ('url', '=', self.url)]
                attach = self.bundle.env['ir.attachment'].sudo().search_read(domain, fields)
                self._ir_attach = attach[0]
            except Exception:
                raise AssetNotFound("Could not find %s" % self.name)

    def to_html(self):
        raise NotImplementedError()

    @func.lazy_property
    def last_modified(self):
        try:
            self.stat()
            if self._filename:
                return datetime.fromtimestamp(os.path.getmtime(self._filename))
            elif self._ir_attach:
                server_format = tools.DEFAULT_SERVER_DATETIME_FORMAT
                last_update = self._ir_attach['__last_update']
                try:
                    return datetime.strptime(last_update, server_format + '.%f')
                except ValueError:
                    return datetime.strptime(last_update, server_format)
        except Exception:
            pass
        return datetime(1970, 1, 1)

    @property
    def content(self):
        if self._content is None:
            self._content = self.inline or self._fetch_content()
        return self._content

    def _fetch_content(self):
        """ Fetch content from file or database"""
        try:
            self.stat()
            if self._filename:
                with open(self._filename, 'rb') as fp:
                    return fp.read().decode('utf-8')
            else:
                return base64.b64decode(self._ir_attach['datas']).decode('utf-8')
        except UnicodeDecodeError:
            raise AssetError('%s is not utf-8 encoded.' % self.name)
        except IOError:
            raise AssetNotFound('File %s does not exist.' % self.name)
        except:
            raise AssetError('Could not get content for %s.' % self.name)

    def minify(self):
        return self.content

    def with_header(self, content=None):
        if content is None:
            content = self.content
        return '\n/* %s */\n%s' % (self.name, content)


class JavascriptAsset(WebAsset):
    def minify(self):
        return self.with_header(rjsmin(self.content))

    def _fetch_content(self):
        try:
            return super(JavascriptAsset, self)._fetch_content()
        except AssetError as e:
            return "console.error(%s);" % json.dumps(str(e))

    def to_html(self):
        if self.url:
            return '<script type="text/javascript" src="%s"></script>' % (self.html_url)
        else:
            return '<script type="text/javascript" charset="utf-8">%s</script>' % self.with_header()


class StylesheetAsset(WebAsset):
    rx_import = re.compile(r"""@import\s+('|")(?!'|"|/|https?://)""", re.U)
    rx_url = re.compile(r"""url\s*\(\s*('|"|)(?!'|"|/|https?://|data:)""", re.U)
    rx_sourceMap = re.compile(r'(/\*# sourceMappingURL=.*)', re.U)
    rx_charset = re.compile(r'(@charset "[^"]+";)', re.U)

    def __init__(self, *args, **kw):
        self.media = kw.pop('media', None)
        super(StylesheetAsset, self).__init__(*args, **kw)

    @property
    def content(self):
        content = super(StylesheetAsset, self).content
        if self.media:
            content = '@media %s { %s }' % (self.media, content)
        return content

    def _fetch_content(self):
        try:
            content = super(StylesheetAsset, self)._fetch_content()
            web_dir = os.path.dirname(self.url)

            if self.rx_import:
                content = self.rx_import.sub(
                    r"""@import \1%s/""" % (web_dir,),
                    content,
                )

            if self.rx_url:
                content = self.rx_url.sub(
                    r"url(\1%s/" % (web_dir,),
                    content,
                )

            if self.rx_charset:
                # remove charset declarations, we only support utf-8
                content = self.rx_charset.sub('', content)

            return content
        except AssetError as e:
            self.bundle.css_errors.append(str(e))
            return ''

    def minify(self):
        # remove existing sourcemaps, make no sense after re-mini
        content = self.rx_sourceMap.sub('', self.content)
        # comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.S)
        # space
        content = re.sub(r'\s+', ' ', content)
        content = re.sub(r' *([{}]) *', r'\1', content)
        return self.with_header(content)

    def to_html(self):
        media = (' media="%s"' % misc.html_escape(self.media)) if self.media else ''
        if self.url:
            href = self.html_url
            return '<link rel="stylesheet" href="%s" type="text/css"%s/>' % (href, media)
        else:
            return '<style type="text/css"%s>%s</style>' % (media, self.with_header())


class PreprocessedCSS(StylesheetAsset):
    rx_import = None

    def __init__(self, *args, **kw):
        super(PreprocessedCSS, self).__init__(*args, **kw)
        self.html_url_format = '%%s/%s/%%s.css' % self.bundle.name
        self.html_url_args = tuple(self.url.rsplit('/', 1))

    def get_source(self):
        content = self.inline or self._fetch_content()
        return "/*! %s */\n%s" % (self.id, content)

    def get_command(self):
        raise NotImplementedError

    def compile(self, source):
        command = self.get_command()
        try:
            compiler = Popen(command, stdin=PIPE, stdout=PIPE,
                             stderr=PIPE)
        except Exception:
            raise CompileError("Could not execute command %r" % command[0])

        (out, err) = compiler.communicate(input=source.encode('utf-8'))
        if compiler.returncode:
            cmd_output = misc.ustr(out) + misc.ustr(err)
            if not cmd_output:
                cmd_output = u"Process exited with return code %d\n" % compiler.returncode
            raise CompileError(cmd_output)
        return out.decode('utf8')

class SassStylesheetAsset(PreprocessedCSS):
    rx_indent = re.compile(r'^( +|\t+)', re.M)
    indent = None
    reindent = '    '

    def minify(self):
        return self.with_header()

    def get_source(self):
        content = textwrap.dedent(self.inline or self._fetch_content())

        def fix_indent(m):
            # Indentation normalization
            ind = m.group()
            if self.indent is None:
                self.indent = ind
                if self.indent == self.reindent:
                    # Don't reindent the file if identation is the final one (reindent)
                    raise StopIteration()
            return ind.replace(self.indent, self.reindent)

        try:
            content = self.rx_indent.sub(fix_indent, content)
        except StopIteration:
            pass
        return "/*! %s */\n%s" % (self.id, content)

    def get_command(self):
        try:
            sass = misc.find_in_path('sass')
        except IOError:
            sass = 'sass'
        return [sass, '--stdin', '-t', 'compressed', '--unix-newlines', '--compass',
                '-r', 'bootstrap-sass']


class ScssStylesheetAsset(PreprocessedCSS):
    @property
    def bootstrap_path(self):
        return get_resource_path('web', 'static', 'lib', 'bootstrap', 'scss')
    @property
    def bootstrap_components_path(self):
        return get_resource_path('web', 'static', 'lib', 'bootstrap', 'scss', 'bootstrap')
    precision = 8
    output_style = 'expanded'

    def compile(self, source):
        if libsass is None:
            return super(ScssStylesheetAsset, self).compile(source)

        try:
            return libsass.compile(
                string=source,
                include_paths=[
                    self.bootstrap_path,
                    self.bootstrap_components_path,
                ],
                output_style=self.output_style,
                precision=self.precision,
            )
        except libsass.CompileError as e:
            raise CompileError(e.args[0])

    def get_command(self):
        try:
            sassc = misc.find_in_path('sassc')
        except IOError:
            sassc = 'sassc'
        return [sassc, '--stdin', '--precision', str(self.precision), '--load-path', self.bootstrap_path, '--load-path', self.bootstrap_components_path, '-t', self.output_style]


class LessStylesheetAsset(PreprocessedCSS):
    def get_command(self):
        try:
            if os.name == 'nt':
                lessc = misc.find_in_path('lessc.cmd')
            else:
                lessc = misc.find_in_path('lessc')
        except IOError:
            lessc = 'lessc'
        lesspath = get_resource_path('web', 'static', 'lib', 'bootstrap', 'less')
        return [lessc, '-', '--no-js', '--no-color', '--include-path=%s' % lesspath]
