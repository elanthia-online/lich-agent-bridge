# frozen_string_literal: true

require 'json'
require_relative 'lab-test-runner'

# Strict, shared controller metadata. The manifest owns controller command
# grammar and bindings; runtime behavior remains in the controller cores and
# the LAB bridge.
module LabControllerRegistry
  NAME = /\A[a-z][a-z0-9_-]{0,63}\z/
  GLOBAL = /\A\$lab_[a-z0-9_]+\z/
  LANES = %w[movement combat inventory communication].freeze
  KINDS = %w[launch stop signal status sync control].freeze
  CONTROLS = %w[status hold resume retreat].freeze
  CATEGORIES = %w[inspection configuration movement combat].freeze
  PARAMETER_TYPES = %w[enum numeric flag_suffix action_id].freeze

  class ManifestError < StandardError; end

  class Parameter
    attr_reader :name, :type, :values, :true_value, :default

    def initialize(raw, context)
      Registry.strict_keys(raw, %w[name type values true_value default], %w[name type], context)
      @name = Registry.name(raw.fetch('name'), "#{context}.name")
      @type = raw.fetch('type').to_s
      raise ManifestError, "#{context}.type is unsupported" unless PARAMETER_TYPES.include?(@type)

      @values = Array(raw['values']).map(&:to_s)
      @true_value = raw.fetch('true_value', '').to_s
      @default = raw.fetch('default', false)
      case @type
      when 'enum'
        raise ManifestError, "#{context}.values must not be empty" if @values.empty?
        raise ManifestError, "#{context}.values contains duplicates" unless @values.map(&:downcase).uniq.length == @values.length
        @values.each { |value| Registry.token(value, "#{context}.values") }
      when 'numeric', 'action_id'
        raise ManifestError, "#{context} #{@type} parameters do not accept values" unless @values.empty?
      when 'flag_suffix'
        raise ManifestError, "#{context}.default must be boolean" unless [true, false].include?(@default)
        raise ManifestError, "#{context}.true_value must begin with a space" unless @true_value.start_with?(' ')
        raise ManifestError, "#{context}.true_value contains control syntax" if @true_value.match?(/[\r\n;|&]/)
      end
    end

    def required?
      @type != 'flag_suffix'
    end

    def pattern
      case @type
      when 'enum' then @values.map { |value| Regexp.escape(value) }.join('|')
      when 'numeric' then '[0-9]+'
      when 'action_id' then '(?-i:[0-9a-f]{16})'
      when 'flag_suffix' then Regexp.escape(@true_value)
      end
    end

    def normalize_capture(value)
      case @type
      when 'enum'
        @values.find { |candidate| candidate.casecmp?(value.to_s) }
      when 'numeric'
        value.to_s
      when 'action_id'
        render(value)
      when 'flag_suffix'
        !value.nil? && !value.empty?
      end
    end

    def render(value)
      case @type
      when 'action_id'
        raise ManifestError, "#{@name} must be a lowercase 16-digit action ID" unless value.is_a?(String) && value.match?(/\A[0-9a-f]{16}\z/)
        value
      when 'enum'
        candidate = value.to_s
        canonical = @values.find { |allowed| allowed.casecmp?(candidate) }
        raise ManifestError, "#{@name} must be one of #{@values.join(', ')}" unless canonical
        canonical
      when 'numeric'
        candidate = value.to_s
        raise ManifestError, "#{@name} must be numeric" unless candidate.match?(/\A[0-9]+\z/)
        candidate
      when 'flag_suffix'
        selected = value.nil? ? @default : value
        raise ManifestError, "#{@name} must be boolean" unless [true, false].include?(selected)
        selected ? @true_value : ''
      end
    end

    def json_schema
      case @type
      when 'enum' then { 'type' => 'string', 'enum' => @values.dup }
      when 'numeric' then { 'type' => 'string', 'pattern' => '^[0-9]+$' }
      when 'action_id' then { 'type' => 'string', 'pattern' => '^[0-9a-f]{16}$' }
      when 'flag_suffix' then { 'type' => 'boolean', 'default' => @default }
      end
    end
  end

  class Action
    attr_reader :name, :kind, :command_template, :script_args_template,
                :launch_mode, :category, :confirmation_required, :parameters,
                :pattern

    def initialize(raw, context)
      Registry.strict_keys(
        raw,
        %w[name kind command_template script_args_template launch_mode policy parameters],
        %w[name kind command_template script_args_template policy parameters],
        context
      )
      @name = Registry.name(raw.fetch('name'), "#{context}.name")
      @kind = raw.fetch('kind').to_s
      raise ManifestError, "#{context}.kind is unsupported" unless KINDS.include?(@kind)
      @command_template = Registry.template(raw.fetch('command_template'), "#{context}.command_template")
      @script_args_template = Registry.template(raw.fetch('script_args_template'), "#{context}.script_args_template", allow_empty: true)
      @launch_mode = raw['launch_mode']&.to_s
      if %w[launch sync].include?(@kind) && !%w[start run].include?(@launch_mode)
        raise ManifestError, "#{context}.launch_mode must be start or run"
      end
      policy = raw.fetch('policy')
      Registry.strict_keys(policy, %w[category confirmation_required], %w[category confirmation_required], "#{context}.policy")
      @category = policy.fetch('category').to_s
      raise ManifestError, "#{context}.policy.category is unsupported" unless CATEGORIES.include?(@category)
      @confirmation_required = policy.fetch('confirmation_required')
      raise ManifestError, "#{context}.policy.confirmation_required must be boolean" unless [true, false].include?(@confirmation_required)
      raw_parameters = raw.fetch('parameters')
      raise ManifestError, "#{context}.parameters must be an array" unless raw_parameters.is_a?(Array)
      @parameters = raw_parameters.each_with_index.map { |item, index| Parameter.new(item, "#{context}.parameters[#{index}]") }
      if @kind == 'control'
        valid = CONTROLS.include?(@name) && @script_args_template.empty? && @launch_mode.nil? &&
                @parameters.length == 1 && @parameters.first.name == 'run_id' && @parameters.first.type == 'action_id' &&
                @category == (@name == 'status' ? 'inspection' : 'combat') && (@name == 'status' || @confirmation_required)
        raise ManifestError, "#{context} has an invalid exact-run control contract" unless valid
      end
      raise ManifestError, "#{context}.parameters contains duplicate names" unless @parameters.map(&:name).uniq.length == @parameters.length
      validate_placeholders(context)
      @pattern = compile_pattern
    end

    def match(command)
      found = @pattern.match(command.to_s)
      return nil unless found

      @parameters.to_h do |parameter|
        [parameter.name, parameter.normalize_capture(found[parameter.name])]
      end
    rescue ManifestError
      nil
    end

    def build(arguments)
      source = arguments.transform_keys(&:to_s)
      allowed = @parameters.map(&:name)
      unknown = source.keys - allowed
      missing = @parameters.select(&:required?).map(&:name) - source.keys
      raise ManifestError, "unsupported argument(s): #{unknown.sort.join(', ')}" unless unknown.empty?
      raise ManifestError, "missing argument(s): #{missing.sort.join(', ')}" unless missing.empty?

      rendered = @parameters.to_h do |parameter|
        [parameter.name, parameter.render(source[parameter.name])]
      end
      {
        command: render_template(@command_template, rendered),
        script_args: render_template(@script_args_template, rendered),
        arguments: @parameters.to_h do |parameter|
          raw = source.key?(parameter.name) ? source[parameter.name] : parameter.default
          normalized = parameter.type == 'flag_suffix' ? !!raw : parameter.render(raw)
          [parameter.name, normalized]
        end
      }
    end

    def argument_schema
      {
        'type' => 'object',
        'properties' => @parameters.to_h { |parameter| [parameter.name, parameter.json_schema] },
        'required' => @parameters.select(&:required?).map(&:name),
        'additionalProperties' => false
      }
    end

    private

    def validate_placeholders(context)
      expected = @parameters.map(&:name).sort
      command_names = @command_template.scan(/\{([a-z][a-z0-9_]*)\}/).flatten.sort
      args_names = @script_args_template.scan(/\{([a-z][a-z0-9_]*)\}/).flatten.sort
      raise ManifestError, "#{context}.command_template placeholders do not match parameters" unless command_names == expected
      unless (args_names - expected).empty? && args_names.uniq.length == args_names.length
        raise ManifestError, "#{context}.script_args_template has invalid placeholders"
      end
    end

    def compile_pattern
      pieces = @command_template.split(/(\{[a-z][a-z0-9_]*\})/)
      source = pieces.map do |piece|
        if (found = piece.match(/\A\{([a-z][a-z0-9_]*)\}\z/))
          parameter = @parameters.find { |item| item.name == found[1] }
          capture = "(?<#{parameter.name}>#{parameter.pattern})"
          parameter.type == 'flag_suffix' ? "(?:#{capture})?" : capture
        else
          Regexp.escape(piece)
        end
      end.join
      Regexp.new("\\A#{source}\\z", Regexp::IGNORECASE)
    end

    def render_template(template, values)
      template.gsub(/\{([a-z][a-z0-9_]*)\}/) { values.fetch(Regexp.last_match(1)) }
    end
  end

  class Controller
    attr_reader :name, :script, :summary, :characters, :result_global,
                :signal_global, :lanes, :owner_scripts, :safe_handoff,
                :capability_action, :actions, :test_suite, :control_owner_scripts

    def initialize(raw, context)
      Registry.strict_keys(
        raw,
        %w[name script summary characters result_global signal_global lanes owner_scripts safe_handoff capability_action actions test_suite control_owner_scripts],
        %w[name script summary characters result_global lanes owner_scripts safe_handoff capability_action actions],
        context
      )
      @name = Registry.name(raw.fetch('name'), "#{context}.name")
      @script = Registry.name(raw.fetch('script'), "#{context}.script")
      @summary = Registry.text(raw.fetch('summary'), "#{context}.summary")
      @characters = Registry.string_array(raw.fetch('characters'), "#{context}.characters")
      @result_global = Registry.global(raw.fetch('result_global'), "#{context}.result_global")
      @signal_global = raw['signal_global'] && Registry.global(raw['signal_global'], "#{context}.signal_global")
      @lanes = Registry.string_array(raw.fetch('lanes'), "#{context}.lanes")
      raise ManifestError, "#{context}.lanes is invalid" unless (@lanes - LANES).empty?
      @owner_scripts = Registry.string_array(raw.fetch('owner_scripts'), "#{context}.owner_scripts")
      @control_owner_scripts = raw.key?('control_owner_scripts') ? Registry.string_array(raw['control_owner_scripts'], "#{context}.control_owner_scripts") : []
      @safe_handoff = raw.fetch('safe_handoff')
      Registry.validate_safe_handoff(@safe_handoff, "#{context}.safe_handoff")
      @capability_action = Registry.name(raw.fetch('capability_action'), "#{context}.capability_action")
      raw_actions = raw.fetch('actions')
      raise ManifestError, "#{context}.actions must be a nonempty array" unless raw_actions.is_a?(Array) && !raw_actions.empty?
      @actions = raw_actions.each_with_index.map { |item, index| Action.new(item, "#{context}.actions[#{index}]") }
      raise ManifestError, "#{context}.actions contains duplicate names" unless @actions.map(&:name).uniq.length == @actions.length
      raise ManifestError, "#{context}.capability_action was not found" unless action(@capability_action)
      if @actions.any? { |item| item.kind == 'control' } && action(@capability_action).kind != 'launch'
        raise ManifestError, "#{context} controls require a launch capability"
      end
      if @actions.any? { |item| item.kind == 'control' }
        allowed = @control_owner_scripts.map(&:downcase)
        unless allowed.include?(@script.downcase) && (allowed - @owner_scripts.map(&:downcase)).empty?
          raise ManifestError, "#{context} requires explicit control_owner_scripts within owner_scripts"
        end
      elsif !@control_owner_scripts.empty?
        raise ManifestError, "#{context}.control_owner_scripts requires registered controls"
      end
      @actions.select { |item| item.kind == 'launch' }.each do |launch|
        tokens = launch.script_args_template.split
        mode = tokens.first.to_s.downcase == 'quick' ? tokens[1].to_s : ''
        may_seek = mode.downcase == 'seek' || launch.parameters.any? do |parameter|
          mode == "{#{parameter.name}}" && parameter.values.map(&:downcase).include?('seek')
        end
        if @script == 'bigshot' && may_seek && (!%w[quick_area quick_refuge].include?(@safe_handoff['kind']) || !(%w[movement combat] - @lanes).empty?)
          raise ManifestError, "#{context}.seek requires a Quick area/refuge contract and movement/combat lanes"
        end
      end
      if %w[quick_area quick_refuge].include?(@safe_handoff['kind'])
        launches = @actions.select { |item| item.kind == 'launch' }
        unless (@safe_handoff['kind'] != 'quick_area' || @safe_handoff.keys == ['kind']) &&
            @script == 'bigshot' && !@control_owner_scripts.empty? && !launches.empty?
          raise ManifestError, "#{context}.#{@safe_handoff['kind']} requires native controlled Bigshot without caller room lists"
        end
        if @safe_handoff['kind'] == 'quick_refuge' && !(%w[movement combat] - @lanes).empty?
          raise ManifestError, "#{context}.quick_refuge requires movement/combat lanes"
        end
        launches.each do |launch|
          tokens = launch.script_args_template.split
          area_options = tokens.each_index.select { |index| tokens[index].start_with?('--area') }
          unless tokens.first == 'quick' && area_options.length == 1 && tokens[area_options.first, 2] == %w[--area profile] &&
              launch.parameters.none? { |parameter| parameter.type == 'flag_suffix' } &&
              tokens.none? { |token| token.start_with?('--supervised-') || (token.start_with?('--') && token.include?('{')) } && !tokens.include?('--')
            raise ManifestError, "#{context}.#{@safe_handoff['kind']} launches require explicit --area profile without private supervisor flags"
          end
        end
      end
      if @safe_handoff['kind'] == 'controller_refuge'
        launches = @actions.select { |item| item.kind == 'launch' }
        unless !@control_owner_scripts.empty? && !launches.empty? && (%w[movement combat] - @lanes).empty?
          raise ManifestError, "#{context}.controller_refuge requires a native controlled script and movement/combat lanes"
        end
        launches.each do |launch|
          tokens = launch.script_args_template.split
          unless !tokens.empty? && launch.parameters.none? { |parameter| parameter.type == 'flag_suffix' } &&
              tokens.none? { |token| token.start_with?('--supervised-') || (token.start_with?('--') && token.include?('{')) } && !tokens.include?('--')
            raise ManifestError, "#{context}.controller_refuge launches cannot supply private supervisor flags"
          end
        end
      end
      raise ManifestError, "#{context}.signal_global is required by signal action" if @actions.any? { |item| item.kind == 'signal' } && !@signal_global
      if raw.key?('test_suite')
        begin
          @test_suite = LabTestRunner.metadata!(raw['test_suite'])
        rescue LabTestRunner::Invalid => error
          raise ManifestError, error.message
        end
        raise ManifestError, 'test suite must use the fixed runner and result globals' unless
          @script == 'lab-test-runner' && @result_global == '$lab_test_result' && @signal_global == '$lab_test_cancel'
        raise ManifestError, 'test suite requires room-bound handoff and exclusion lanes' unless
          @safe_handoff['kind'] == 'room' && @lanes.sort == %w[combat movement]
        raise ManifestError, 'test suite only supports a registered launch' unless
          @actions.length == 1 && @actions.first.kind == 'launch' && @actions.first.launch_mode == 'start'
        suite_id = @name.delete_prefix('test-')
        raise ManifestError, 'test registration must bind one character and fixed capability' unless
          @name.start_with?('test-') && LabTestRunner::ID.match?(suite_id) && @characters.length == 1 &&
          @capability_action == 'start' && @owner_scripts.include?('lab-test-runner')
        launch = @actions.first
        params = launch.parameters.to_h { |parameter| [parameter.name, parameter] }
        raise ManifestError, 'invalid test launch contract' unless launch.name == 'start' &&
          launch.category == 'configuration' && launch.confirmation_required &&
          launch.command_template == "lab-test #{suite_id} {revision} {case_id}" &&
          launch.script_args_template == "#{suite_id} {revision} {case_id}" &&
          params.keys.sort == %w[case_id revision] && params['revision'].type == 'enum' &&
          params['revision'].values == [@test_suite['files'][@test_suite['manifest']]] &&
          params['case_id'].type == 'enum' && params['case_id'].values.include?('all') &&
          params['case_id'].values.length.between?(2, 21)
      end
    end

    def action(name)
      @actions.find { |item| item.name == name.to_s }
    end
  end

  Match = Struct.new(:controller, :action, :arguments, :script_args, keyword_init: true)

  class Registry
    attr_reader :controllers, :path

    def self.load(path)
      raw = JSON.parse(File.read(path, encoding: 'UTF-8'))
      strict_keys(raw, %w[version controllers], %w[version controllers], 'manifest')
      raise ManifestError, 'manifest.version must be 1' unless raw.fetch('version') == 1
      controllers = raw.fetch('controllers')
      raise ManifestError, 'manifest.controllers must be an array' unless controllers.is_a?(Array)
      new(controllers.each_with_index.map { |item, index| Controller.new(item, "controllers[#{index}]") }, path)
    rescue JSON::ParserError => error
      raise ManifestError, "invalid controller manifest JSON: #{error.message}"
    end

    def initialize(controllers, path)
      @controllers = controllers.freeze
      @path = path.to_s
      raise ManifestError, 'controller names must be unique' unless @controllers.map(&:name).uniq.length == @controllers.length
      raise ManifestError, 'controller scripts must be unique' unless @controllers.map(&:script).uniq.length == @controllers.length
    end

    def controller(name)
      @controllers.find { |item| item.name == name.to_s.downcase }
    end

    def match(command)
      @controllers.each do |controller|
        controller.actions.each do |action|
          arguments = action.match(command)
          next unless arguments
          built = action.build(arguments)
          return Match.new(
            controller: controller,
            action: action,
            arguments: built.fetch(:arguments),
            script_args: built.fetch(:script_args)
          )
        end
      end
      nil
    end

    def safe_patterns
      @controllers.flat_map(&:actions).map(&:pattern).freeze
    end

    def controller_scripts_for_lane(lane)
      @controllers.select { |controller| controller.lanes.include?(lane.to_s) }.map(&:script)
    end

    class << self
      def strict_keys(raw, allowed, required, context)
        raise ManifestError, "#{context} must be an object" unless raw.is_a?(Hash)
        unknown = raw.keys - allowed
        missing = required - raw.keys
        raise ManifestError, "#{context} has unsupported field(s): #{unknown.sort.join(', ')}" unless unknown.empty?
        raise ManifestError, "#{context} is missing field(s): #{missing.sort.join(', ')}" unless missing.empty?
      end

      def name(value, context)
        text = value.to_s
        raise ManifestError, "#{context} is invalid" unless NAME.match?(text)
        text
      end

      def global(value, context)
        text = value.to_s
        raise ManifestError, "#{context} is invalid" unless GLOBAL.match?(text)
        text
      end

      def text(value, context)
        result = value.to_s.strip
        raise ManifestError, "#{context} must not be blank" if result.empty?
        raise ManifestError, "#{context} is too long" if result.length > 500
        result
      end

      def token(value, context)
        result = value.to_s
        raise ManifestError, "#{context} is invalid" unless result.match?(/\A[a-z0-9][a-z0-9_-]{0,127}\z/i)
        result
      end

      def template(value, context, allow_empty: false)
        result = value.to_s
        raise ManifestError, "#{context} must not be blank" if !allow_empty && result.empty?
        raise ManifestError, "#{context} is too long" if result.length > 300
        raise ManifestError, "#{context} contains control syntax" if result.match?(/[\r\n;|&]/)
        result
      end

      def string_array(value, context)
        raise ManifestError, "#{context} must be a nonempty array" unless value.is_a?(Array) && !value.empty?
        result = value.map { |item| token(item, context) }
        raise ManifestError, "#{context} contains duplicates" unless result.map(&:downcase).uniq.length == result.length
        result.freeze
      end

      def validate_safe_handoff(raw, context)
        if raw.is_a?(Hash) && %w[quick_refuge controller_refuge].include?(raw['kind'])
          strict_keys(raw, %w[kind room_id return_seconds], %w[kind room_id return_seconds], context)
          room = raw['room_id']
          unless (room.is_a?(Integer) || room.is_a?(String)) && room.to_s.match?(/\A[0-9]+\z/) && room.to_i.positive? && room.to_i != 4
            raise ManifestError, "#{context}.room_id must be a positive room other than 4"
          end
          unless raw['return_seconds'].is_a?(Integer) && raw['return_seconds'].between?(10, 120)
            raise ManifestError, "#{context}.return_seconds must be an integer from 10 through 120"
          end
          return
        end
        strict_keys(raw, %w[kind room_id rooms], %w[kind], context)
        kind = raw.fetch('kind').to_s
        raise ManifestError, "#{context}.kind is unsupported" unless %w[owners_released room profile_room quick_area].include?(kind)
        if kind == 'room'
          raise ManifestError, "#{context}.room_id must be numeric" unless raw['room_id'].to_s.match?(/\A[0-9]+\z/)
        elsif kind == 'profile_room'
          rooms = raw['rooms']
          raise ManifestError, "#{context}.rooms must be a nonempty object" unless rooms.is_a?(Hash) && !rooms.empty?
          rooms.each do |profile, room_id|
            token(profile, "#{context}.rooms profile")
            raise ManifestError, "#{context}.rooms must contain numeric room IDs" unless room_id.to_s.match?(/\A[0-9]+\z/)
          end
        end
      end
    end
  end

  module_function

  def default_path
    ENV.fetch('LAB_CONTROLLER_MANIFEST', File.join(__dir__, 'lab-controllers.json'))
  end

  def load(path = default_path)
    Registry.load(path)
  end
end
