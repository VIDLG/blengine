# frozen_string_literal: true

# Read-only snapshots for the canonical Python build owner. No Podfile/Ruby
# Podspec evaluation, installer, subprocess, network access, or receipt writing.
require 'cocoapods'
require 'json'
require 'digest'
require 'pathname'
require 'set'

module IosPodGraph
  MAX_FILES = 100_000
  MAX_VISITS = 250_000
  MAX_FILE_BYTES = 16 * 1024 * 1024
  MAX_HASH_BYTES = 256 * 1024 * 1024
  MAX_OUTPUT_BYTES = 16 * 1024 * 1024
  MAX_SPECS = 512
  EXCLUDED = %w[node_modules build .work .git Pods .gradle .cxx .cache coverage].freeze
  SOURCE_EXTENSIONS = %w[.js .jsx .ts .tsx .mts .cts .mjs .cjs .json .rb .podspec .sh].freeze

  class Snapshot
    def initialize(ios, host, staging)
      @ios, @host, @staging = [ios, host, staging].map { |p| directory(Pathname.new(p).expand_path) }
      raise 'ios_root must be within host_root' unless within?(@ios, @host)
      @visits = @files = @hash_bytes = 0
      @hashes = {}
    end

    def within?(path, root)
      path == root || path.to_s.start_with?(root.to_s + '/')
    end

    def directory(path)
      raise "missing directory: #{path}" unless path.directory?
      path.realpath
    end

    def visit!
      @visits += 1
      raise 'filesystem traversal limit exceeded' if @visits > MAX_VISITS
    end

    def member(path, root = nil)
      visit!
      stat = path.lstat
      resolved = path.realpath
      raise "member escapes root: #{path}" if root && !within?(resolved, root)
      raise "special file: #{path}" unless resolved.file? || resolved.directory?
      { 'path' => path.expand_path.to_s, 'realpath' => resolved.to_s,
        'kind' => resolved.directory? ? 'directory' : 'file', 'symlink' => stat.symlink? }
    end

    def digest(path)
      key = path.expand_path.to_s
      return @hashes[key] if @hashes.key?(key)
      info = member(path)
      raise "expected regular file: #{path}" unless info['kind'] == 'file'
      before = path.stat
      raise "file too large: #{path}" if before.size > MAX_FILE_BYTES
      @files += 1
      @hash_bytes += before.size
      raise 'metadata file count limit exceeded' if @files > MAX_FILES
      raise 'metadata byte limit exceeded' if @hash_bytes > MAX_HASH_BYTES
      bytes = path.binread(MAX_FILE_BYTES + 1)
      raise "file too large: #{path}" if bytes.bytesize > MAX_FILE_BYTES
      sha = Digest::SHA256.hexdigest(bytes)
      after = path.stat
      unless [before.dev, before.ino, before.size, before.mtime, before.ctime] ==
             [after.dev, after.ino, after.size, after.mtime, after.ctime] && path.realpath.to_s == info['realpath']
        raise "file changed while reading: #{path}"
      end
      @hashes[key] = info.merge('sha256' => sha)
    end

    def read_bytes(path)
      expected = digest(path)
      bytes = path.binread(MAX_FILE_BYTES + 1)
      raise "file too large: #{path}" if bytes.bytesize > MAX_FILE_BYTES
      raise "file changed while reading: #{path}" unless Digest::SHA256.hexdigest(bytes) == expected['sha256']
      bytes
    end

    def read_json(path)
      JSON.parse(read_bytes(path))
    end

    # XCFramework slice order is not stable across xcodebuild rewrites (React
    # Native's prebuilt Core pod reassembles its framework during the build).
    # Hash the semantic content instead: every field and value is retained,
    # only AvailableLibraries entries are ordered by their unique identifier.
    def semantic_plist_digest(path)
      record = digest(path)
      text = read_bytes(path).dup.force_encoding(Encoding::UTF_8)
      raise "invalid XCFramework Info.plist: #{path}" unless text.valid_encoding?
      marker = '<key>AvailableLibraries</key>'
      return record unless text.include?(marker)
      head, rest = text.split(marker, 2)
      open_index = rest.index('<array>')
      raise "malformed XCFramework Info.plist: #{path}" if open_index.nil?
      entries_start = open_index + '<array>'.length
      # Each slice contains nested <array> elements (architectures), so the
      # outer array close must be found by depth, not by the first close tag.
      depth = 1
      close_index = nil
      cursor = entries_start
      while (tag = rest.match(%r{</?array>}, cursor))
        if tag[0] == '<array>'
          depth += 1
        else
          depth -= 1
          if depth.zero?
            close_index = tag.begin(0)
            break
          end
        end
        cursor = tag.end(0)
      end
      raise "malformed XCFramework Info.plist: #{path}" if close_index.nil?
      inner = rest[entries_start...close_index]
      entries = inner.scan(%r{<dict>.*?</dict>}m)
      unless entries.join.gsub(/\s+/, '') == inner.gsub(/\s+/, '')
        raise "unsupported XCFramework library layout: #{path}"
      end
      identifiers = entries.map do |entry|
        value = entry[%r{<key>LibraryIdentifier</key>\s*<string>([^<]*)</string>}, 1]
        raise "missing XCFramework LibraryIdentifier: #{path}" if value.nil? || value.empty?
        value
      end
      raise "duplicate XCFramework LibraryIdentifier: #{path}" if identifiers.uniq.size != identifiers.size
      ordered = entries.zip(identifiers).sort_by { |_entry, identifier| identifier }.map(&:first)
      canonical = head + marker + rest[0...entries_start] + ordered.join("\n") + rest[close_index..]
      record.merge('sha256' => Digest::SHA256.hexdigest(canonical.b))
    end

    # Resolve package-root links explicitly. Interior links may only stay within
    # the scanned root, with a per-branch cycle check and a shared traversal cap.
    def walk(root, excluded: EXCLUDED, directory_links: false, include_directories: false, &block)
      boundary = root.realpath
      stack = [[root, Set.new([boundary])]]
      until stack.empty?
        dir, ancestors = stack.pop
        dir.children.sort.reverse_each do |path|
          visit!
          next if excluded.include?(path.basename.to_s)
          if path.symlink?
            member(path, boundary)
            if path.directory?
              raise "interior directory symlink is unsupported: #{path}" unless directory_links
              resolved = path.realpath
              raise "directory symlink cycle: #{path}" if ancestors.include?(resolved)
              block.call(path) if include_directories
              stack << [path, ancestors | Set.new([resolved])]
            else
              block.call(path)
            end
          elsif path.directory?
            block.call(path) if include_directories
            stack << [path, ancestors | Set.new([path.realpath])]
          elsif path.file?
            block.call(path)
          else
            raise "special file: #{path}"
          end
        end
      end
    end

    def packages(base = @host, require_react_native: true)
      hidden = base + 'node_modules/.package-lock.json'
      data = read_json(hidden).fetch('packages')
      raise 'npm installed packages must be an object' unless data.is_a?(Hash)
      raise 'npm package count limit exceeded' if data.size > 4096
      result = []
      data.keys.sort.each do |relative|
        # npm also records the external destination of a file: link. Its
        # node_modules entry below supplies the logical identity we must bind.
        next unless relative.start_with?('node_modules/')
        path = Pathname.new(relative)
        raise "unsafe npm package path: #{relative}" if path.absolute? || path.each_filename.include?('..')
        logical = base + path
        root = directory(logical)
        raise "missing package.json: #{logical}" unless (root + 'package.json').file?
        result << { 'path' => logical.to_s, 'realpath' => root.to_s }
      end
      if require_react_native && !result.any? { |p| p['path'] == (base + 'node_modules/react-native').to_s }
        raise 'missing installed React Native package'
      end
      result
    end

    def nitrogen_inputs(root, package, metadata)
      # bleng-check runs this package's npm generate script, not a generator
      # from the host's node_modules. Only its existing default invocation is
      # supported here; never execute npm, resolve through PATH, or regenerate.
      unless package.fetch('scripts').fetch('generate') == 'nitrogen'
        raise 'unsupported formal Nitrogen invocation'
      end
      config = root + 'nitro.json'
      read_json(config)
      %w[nitro.json package-lock.json node_modules/.package-lock.json].each do |name|
        path = root + name
        metadata[path.to_s] = digest(path)
      end
      # Include imported TS types/helpers as well as *.nitro.ts membership.
      # Nitrogen's default base is the package root; native build directories
      # cannot be skipped if they contain schema inputs. Generated native bytes
      # are not hashed, and node_modules is covered separately below.
      walk(root, excluded: %w[node_modules .git], directory_links: true) do |path|
        if %w[.ts .tsx .mts .cts .js .jsx .mjs .cjs].include?(path.extname) || path.basename.to_s == 'nitro.json'
          metadata[path.to_s] = digest(path)
        end
      end
      generator = directory(root + 'node_modules/nitrogen')
      generator_package = read_json(generator + 'package.json')
      executable = root + 'node_modules/.bin/nitrogen'
      bin = generator_package.fetch('bin')
      bin = bin.fetch('nitrogen') if bin.is_a?(Hash)
      raise 'unsupported Nitrogen executable' unless bin.is_a?(String)
      expected = (generator + bin).realpath
      unless within?(expected, generator) && executable.realpath == expected
        raise 'Nitrogen executable does not match its installed package'
      end
      metadata[executable.to_s] = digest(executable)
      # Bind the installed generator environment (including transitive runtime
      # dependencies and imported declarations), not merely a version string.
      # Use the same bounded npm inventory as the host; no second install owner.
      packages(root, require_react_native: false).each do |entry|
        logical = Pathname.new(entry['path'])
        metadata[(logical + 'package.json').to_s] = digest(logical + 'package.json')
        walk(logical, directory_links: true) do |path|
          metadata[path.to_s] = digest(path) if SOURCE_EXTENSIONS.include?(path.extname)
        end
      end
    end

    def declared
      inventory = packages
      roots = [@host, @staging] + inventory.map { |p| Pathname.new(p['realpath']) }
      metadata = {}
      roots.uniq.sort.each do |root|
        walk(root, directory_links: true) do |path|
          name = path.basename.to_s
          if name == 'package.json' || name == 'react-native.config.js' ||
             name == 'react-native.config.cjs' || name == 'react-native.config.mjs' ||
             path.extname == '.podspec' || path.extname == '.rb' || name.end_with?('.podspec.json') ||
             (name == 'Info.plist' && path.parent.extname == '.xcframework') ||
             path.to_s.end_with?('/sdks/hermes-engine/version.properties')
            metadata[path.to_s] = digest(path)
          end
        end
      end
      %w[package.json package-lock.json node_modules/.package-lock.json].each do |name|
        path = @host + name
        metadata[path.to_s] = digest(path)
      end
      metadata[(@ios + 'Podfile').to_s] = digest(@ios + 'Podfile')
      # Hash the generator implementation AND schema inputs, not ordinary
      # Swift/Kotlin/C++ source bytes. Additions/removals change these maps.
      codegen_roots = inventory.filter_map do |entry|
        root = Pathname.new(entry['realpath'])
        package = read_json(root + 'package.json')
        nitrogen_inputs(root, package, metadata) if package['name'] == '@blengine/react-native-bleng'
        config = package['codegenConfig']
        if config
          raise "unsupported codegenConfig: #{root}" unless config.is_a?(Hash)
          directories = [config['jsSrcsDir']]
          if config['libraries']
            raise 'unsupported codegen libraries' unless config['libraries'].is_a?(Array)
            directories += config['libraries'].map { |lib| lib.fetch('jsSrcsDir') }
          end
          directories.compact.each do |relative|
            raise "invalid codegen source directory: #{root}" unless relative.is_a?(String)
            source_root = directory(root + relative)
            raise "codegen source escapes package: #{root}" unless within?(source_root, root)
            walk(source_root) { |path| metadata[path.to_s] = digest(path) if SOURCE_EXTENSIONS.include?(path.extname) }
          end
        end
        next unless entry['path'].end_with?('/node_modules/react-native', '/node_modules/@react-native/codegen', '/node_modules/nitrogen') || entry['path'].match?(%r{/node_modules/@react-native-community/cli[^/]*$})
        entry['path'].end_with?('/node_modules/react-native') ? root + 'scripts' : root
      end
      host_package = read_json(@host + 'package.json')
      raise 'host codegenConfig requires explicit package source coverage' if host_package['codegenConfig']
      codegen_roots.uniq.sort.each do |root|
        walk(directory(root)) { |path| metadata[path.to_s] = digest(path) if SOURCE_EXTENSIONS.include?(path.extname) }
      end
      { 'roots' => { 'ios' => @ios.to_s, 'host' => @host.to_s, 'staging' => @staging.to_s },
        'packages' => inventory, 'metadata' => metadata.sort.to_h }
    end

    def source_paths(accessor, attribute)
      paths = Array(accessor.public_send(attribute)).flatten.compact
      raise 'expanded source count limit exceeded' if paths.size > MAX_FILES
      result = {}
      paths.each do |path|
        path = Pathname.new(path)
        record = member(path)
        result[record['path']] = record
        next unless record['kind'] == 'directory'
        # Bundle membership matters even when its top-level path is unchanged.
        walk(path, excluded: [], directory_links: true, include_directories: true) do |child|
          record = member(child, path.realpath)
          result[record['path']] = record
          raise 'expanded source count limit exceeded' if result.size > MAX_FILES
        end
        if attribute == :vendored_frameworks && path.extname == '.xcframework'
          # CocoaPods bakes slice selection from this metadata into its support
          # scripts. Library/source bytes may change without reinstallation.
          info = path + 'Info.plist'
          result[info.expand_path.to_s] = semantic_plist_digest(info)
        end
      end
      result.sort.to_h
    end

    def installed
      pods = directory(@ios + 'Pods') # Sandbox.new must not create a missing sandbox.
      lock_path = @ios + 'Podfile.lock'
      manifest_path = pods + 'Manifest.lock'
      lock_hash, manifest_hash = digest(lock_path), digest(manifest_path)
      raise 'Podfile.lock and Pods/Manifest.lock differ' unless lock_path.binread == manifest_path.binread
      sandbox = Pod::Sandbox.new(pods)
      manifest = sandbox.manifest
      raise 'missing CocoaPods manifest' unless manifest
      data = manifest.to_hash
      raise 'installed CocoaPods version differs from lock' unless manifest.cocoapods_version.to_s == Pod::VERSION
      raise 'Podfile checksum differs from installed manifest' unless Digest::SHA1.file(@ios + 'Podfile').hexdigest == data.fetch('PODFILE CHECKSUM')
      names = manifest.pod_names.sort
      raise 'installed specification count limit exceeded' if names.empty? || names.size > MAX_SPECS
      external = data.fetch('EXTERNAL SOURCES')
      roots = names.map { |n| Pod::Specification.root_name(n) }.uniq.sort
      raise 'non-local or unaccounted installed Pods are unsupported' unless external.keys.sort == roots
      declared_roots = [@host, @staging] + packages.map { |p| Pathname.new(p['realpath']) }
      specifications = {}
      compile_sets = {}
      resource_sets = {}
      all_sources = Set.new
      scanned_source_roots = Set.new
      roots.each do |name|
        options = external.fetch(name)
        installed_path = sandbox.specification_path(name)
        raise "installed specification must be JSON: #{name}" unless installed_path && installed_path.extname == '.json'
        # Bound/read the JSON before parsing it. Do not register development
        # paths or call Sandbox#specification, which could evaluate originals.
        installed_bytes = read_bytes(installed_path)
        spec = Pod::Specification.from_json(installed_bytes, installed_path.to_s)
        raise "installed specification identity mismatch: #{name}" unless spec.name == name && spec.version == manifest.version(name)
        raise "installed spec checksum mismatch: #{name}" unless Digest::SHA1.hexdigest(installed_bytes) == manifest.checksum(name)
        if options[:path]
          logical_root = (@ios + options.fetch(:path)).expand_path
          source_root = directory(logical_root)
          definitions = [logical_root + "#{name}.podspec", logical_root + "#{name}.podspec.json"].select(&:file?)
          raise "ambiguous/missing local specification: #{name}" unless definitions.size == 1
          definition = definitions.first
        elsif options[:podspec]
          value = options.fetch(:podspec)
          raise "remote podspec is unsupported: #{name}" unless value.is_a?(String) && !value.include?('://')
          definition = (@ios + value).expand_path
          source_root = directory(pods + name)
          logical_root = pods + name
        else
          raise "unsupported external source: #{name}"
        end
        # Generated definitions are post-install evidence, not before/after
        # declared input. All other definitions must have pre-install coverage.
        generated = within?(definition.realpath, @ios + 'build')
        covered = declared_roots.any? do |root|
          within?(definition.realpath, root) &&
            (definition.realpath.relative_path_from(root).each_filename.to_a & EXCLUDED).empty?
        end
        unless generated || covered
          raise "local definition has no declared input coverage: #{definition}"
        end
        # Bound the filesystem before CocoaPods' PathList performs its own glob
        # expansion. A fresh PathList is scoped to this snapshot, never cached
        # across verification calls.
        unless scanned_source_roots.include?(source_root)
          walk(source_root, excluded: [], directory_links: true) { |_path| }
          scanned_source_roots.add(source_root)
        end
        selected = names.select { |n| Pod::Specification.root_name(n) == name }
        compile_sets[name] = Set.new
        resource_sets[name] = Set.new
        accessors = selected.to_h do |selected_name|
          consumer = spec.subspec_by_name(selected_name).consumer(:ios)
          accessor = Pod::Sandbox::FileAccessor.new(source_root, consumer)
          all_sources.merge(accessor.source_files.map { |p| p.realpath.to_s })
          compile_sets[name].merge((accessor.source_files - accessor.headers - accessor.other_source_files).map { |p| p.realpath.to_s })
          attributes = %i[source_files headers public_headers project_headers private_headers arc_source_files non_arc_source_files other_source_files resources resource_bundle_files vendored_frameworks vendored_libraries]
          expanded = attributes.to_h { |attribute| [attribute.to_s, source_paths(accessor, attribute)] }
          %i[resources resource_bundle_files].each do |attribute|
            expanded.fetch(attribute.to_s).each_value do |record|
              resource_sets[name].add(record['realpath'])
            end
          end
          %i[module_map prefix_header].each do |attribute|
            value = accessor.public_send(attribute)
            expanded[attribute.to_s] = value ? member(value) : nil
          end
          [selected_name, expanded]
        end
        specifications[name] = { 'source_root' => source_root.to_s, 'logical_root' => logical_root.to_s,
                                 'definition' => digest(definition), 'installed_json' => digest(installed_path),
                                 'selected' => accessors }
      end
      project_path = pods + 'Pods.xcodeproj'
      # Xcodeproj opens project.pbxproj itself; preflight its bytes before that
      # unbounded library read. The owner serializes against other writers.
      digest(project_path + 'project.pbxproj')
      project = Xcodeproj::Project.open(project_path)
      if project.objects.any? { |o| o.isa.start_with?('PBXFileSystemSynchronized') }
        raise 'synchronized Pods groups are unsupported'
      end
      referenced = project.objects.select { |o| o.isa == 'PBXFileReference' && o.real_path.exist? }.map { |o| o.real_path.realpath.to_s }.to_set
      absent = all_sources - referenced
      raise "expanded source missing from Pods project: #{absent.first}" unless absent.empty?
      compile_sets.each do |name, expected|
        candidates = project.targets.select { |t| t.name == name || t.name.start_with?(name + '-', name + '.') }
        actual = Set.new
        resources = Set.new
        candidates.each do |target|
          next unless target.isa == 'PBXNativeTarget'
          phases = target.build_phases.select { |p| p.isa == 'PBXSourcesBuildPhase' }
          raise "ambiguous source phases: #{target.name}" if phases.size > 1
          resource_phases = target.build_phases.select { |p| p.isa == 'PBXResourcesBuildPhase' }
          raise "ambiguous resource phases: #{target.name}" if resource_phases.size > 1
          resource_phases.each do |phase|
            phase.files.each do |file|
              raise "unresolved resource reference: #{target.name}" unless file.file_ref&.respond_to?(:real_path)
              resources.add(file.file_ref.real_path.realpath.to_s)
            end
          end
          phases.each do |phase|
            phase.files.each do |file|
              raise "unresolved source reference: #{target.name}" unless file.file_ref&.respond_to?(:real_path)
              path = file.file_ref.real_path
              dummy = pods + "Target Support Files/#{target.name}/#{target.name}-dummy.m"
              next if path.expand_path == dummy.expand_path && path.file?
              actual.add(path.realpath.to_s)
            end
          end
        end
        # Unsupported resource-compilation or target-variant mappings fail
        # conservatively, rather than sealing a partially integrated graph.
        raise "compiled source membership differs: #{name}" unless actual == expected
        expected_resources = resource_sets.fetch(name)
        unless expected_resources.empty? || expected_resources == resources
          raise "resource membership differs: #{name}"
        end
      end
      targets = project.targets.sort_by(&:name).map do |target|
        phases = target.build_phases.map do |phase|
          files = phase.respond_to?(:files) ? phase.files.map do |file|
            ref = file.file_ref
            record = { 'uuid' => file.uuid, 'settings' => file.settings, 'reference' => ref&.uuid }
            if ref && ref.respond_to?(:real_path)
              path = ref.real_path
              record['path'] = path.to_s
              if %w[PBXSourcesBuildPhase PBXHeadersBuildPhase PBXResourcesBuildPhase].include?(phase.isa)
                record['member'] = member(path)
              end
            end
            record
          end : []
          { 'isa' => phase.isa, 'uuid' => phase.uuid, 'files' => files }
        end
        { 'name' => target.name, 'isa' => target.isa, 'phases' => phases }
      end
      integration = {}
      [project_path, pods + 'Target Support Files', @ios + 'build/generated'].each do |root|
        walk(directory(root), excluded: []) do |path|
          # Generated source bytes are intentionally not preparation inputs;
          # their membership is captured through FileAccessor/project phases.
          next if within?(path, @ios + 'build/generated') && !%w[.json .rb .podspec].include?(path.extname)
          integration[path.to_s] = digest(path)
        end
      end
      @ios.children.sort.each do |path|
        next unless %w[.xcodeproj .xcworkspace].include?(path.extname)
        walk(directory(path)) { |child| integration[child.to_s] = digest(child) }
      end
      autolinking = @ios + 'build/generated/autolinking/autolinking.json'
      integration[autolinking.to_s] = digest(autolinking)
      %w[.xcode.env .xcode.env.local].each do |name|
        path = @ios + name
        integration[path.to_s] = digest(path) if path.exist?
      end
      # The lock files must still be the bytes read before inspecting the graph.
      raise 'lock changed during snapshot' unless Digest::SHA256.file(lock_path).hexdigest == lock_hash['sha256'] && Digest::SHA256.file(manifest_path).hexdigest == manifest_hash['sha256']
      { 'roots' => { 'ios' => @ios.to_s, 'host' => @host.to_s, 'staging' => @staging.to_s },
        'toolchain' => { 'cocoapods' => Pod::VERSION, 'xcodeproj' => Xcodeproj::VERSION, 'ruby' => RUBY_VERSION,
                         'lock_cocoapods' => manifest.cocoapods_version.to_s },
        'lock' => lock_hash, 'manifest' => manifest_hash, 'podfile' => digest(@ios + 'Podfile'),
        'specifications' => specifications, 'targets' => targets, 'integration' => integration.sort.to_h }
    end
  end

  def self.main(arguments)
    raise 'usage: ios_pod_graph.rb inputs|installed IOS_ROOT HOST_ROOT STAGING_ROOT' unless arguments.size == 4 && %w[inputs installed].include?(arguments.first)
    action, ios, host, staging = arguments
    snapshot = Snapshot.new(ios, host, staging)
    result = action == 'inputs' ? snapshot.declared : snapshot.installed
    output = JSON.generate({ 'schema' => 1, 'kind' => action, 'snapshot' => result })
    raise 'snapshot output limit exceeded' if output.bytesize > MAX_OUTPUT_BYTES
    puts output
  end
end

if $PROGRAM_NAME == __FILE__
  begin
    IosPodGraph.main(ARGV)
  rescue StandardError => error
    warn "ios_pod_graph: #{error.class}: #{error.message}"
    exit 1
  end
end
