"""Semantic analysis of types"""

import itertools
from itertools import chain
from contextlib import contextmanager
from collections import OrderedDict

from typing import Callable, List, Optional, Set, Tuple, Iterator, TypeVar, Iterable
from typing_extensions import Final
from mypy_extensions import DefaultNamedArg

from mypy.messages import MessageBuilder, quote_type_string, format_type_bare
from mypy.options import Options
from mypy.types import (
    Type, UnboundType, TypeVarType, TupleType, TypedDictType, UnionType, Instance, AnyType,
    CallableType, NoneType, DeletedType, TypeList, TypeVarDef, SyntheticTypeVisitor,
    StarType, PartialType, EllipsisType, UninhabitedType, TypeType, get_typ_args, set_typ_args,
    CallableArgument, get_type_vars, TypeQuery, union_items, TypeOfAny,
    LiteralType, RawExpressionType, PlaceholderType
)

from mypy.nodes import (
    TypeInfo, Context, SymbolTableNode, Var, Expression,
    nongen_builtins, check_arg_names, check_arg_kinds, ARG_POS, ARG_NAMED,
    ARG_OPT, ARG_NAMED_OPT, ARG_STAR, ARG_STAR2, TypeVarExpr,
    TypeAlias, PlaceholderNode, SYMBOL_FUNCBASE_TYPES, Decorator, MypyFile
)
from mypy.typetraverser import TypeTraverserVisitor
from mypy.tvar_scope import TypeVarScope
from mypy.exprtotype import expr_to_unanalyzed_type, TypeTranslationError
from mypy.plugin import Plugin, TypeAnalyzerPluginInterface, AnalyzeTypeContext
from mypy.semanal_shared import SemanticAnalyzerCoreInterface
from mypy.errorcodes import ErrorCode
from mypy import nodes, message_registry, errorcodes as codes

T = TypeVar('T')

type_constructors = {
    'typing.Callable',
    'typing.Optional',
    'typing.Tuple',
    'typing.Type',
    'typing.Union',
    'typing.Literal',
    'typing_extensions.Literal',
    'typing_extensions.Annotated',
}  # type: Final

ARG_KINDS_BY_CONSTRUCTOR = {
    'mypy_extensions.Arg': ARG_POS,
    'mypy_extensions.DefaultArg': ARG_OPT,
    'mypy_extensions.NamedArg': ARG_NAMED,
    'mypy_extensions.DefaultNamedArg': ARG_NAMED_OPT,
    'mypy_extensions.VarArg': ARG_STAR,
    'mypy_extensions.KwArg': ARG_STAR2,
}  # type: Final


def analyze_type_alias(node: Expression,
                       api: SemanticAnalyzerCoreInterface,
                       tvar_scope: TypeVarScope,
                       plugin: Plugin,
                       options: Options,
                       is_typeshed_stub: bool,
                       allow_unnormalized: bool = False,
                       allow_placeholder: bool = False,
                       in_dynamic_func: bool = False,
                       global_scope: bool = True) -> Optional[Tuple[Type, Set[str]]]:
    """Analyze r.h.s. of a (potential) type alias definition.

    If `node` is valid as a type alias rvalue, return the resulting type and a set of
    full names of type aliases it depends on (directly or indirectly).
    Return None otherwise. 'node' must have been semantically analyzed.
    """
    try:
        type = expr_to_unanalyzed_type(node)
    except TypeTranslationError:
        api.fail('Invalid type alias: expression is not a valid type', node)
        return None
    analyzer = TypeAnalyser(api, tvar_scope, plugin, options, is_typeshed_stub,
                            allow_unnormalized=allow_unnormalized, defining_alias=True,
                            allow_placeholder=allow_placeholder)
    analyzer.in_dynamic_func = in_dynamic_func
    analyzer.global_scope = global_scope
    res = type.accept(analyzer)
    return res, analyzer.aliases_used


def no_subscript_builtin_alias(name: str, propose_alt: bool = True) -> str:
    msg = '"{}" is not subscriptable'.format(name.split('.')[-1])
    replacement = nongen_builtins[name]
    if replacement and propose_alt:
        msg += ', use "{}" instead'.format(replacement)
    return msg


class TypeAnalyser(SyntheticTypeVisitor[Type], TypeAnalyzerPluginInterface):
    """Semantic analyzer for types.

    Converts unbound types into bound types. This is a no-op for already
    bound types.

    If an incomplete reference is encountered, this does a defer. The
    caller never needs to defer.
    """

    # Is this called from an untyped function definition?
    in_dynamic_func = False  # type: bool
    # Is this called from global scope?
    global_scope = True  # type: bool

    def __init__(self,
                 api: SemanticAnalyzerCoreInterface,
                 tvar_scope: TypeVarScope,
                 plugin: Plugin,
                 options: Options,
                 is_typeshed_stub: bool, *,
                 defining_alias: bool = False,
                 allow_tuple_literal: bool = False,
                 allow_unnormalized: bool = False,
                 allow_unbound_tvars: bool = False,
                 allow_placeholder: bool = False,
                 report_invalid_types: bool = True) -> None:
        self.api = api
        self.lookup_qualified = api.lookup_qualified
        self.lookup_fqn_func = api.lookup_fully_qualified
        self.fail_func = api.fail
        self.note_func = api.note
        self.tvar_scope = tvar_scope
        # Are we analysing a type alias definition rvalue?
        self.defining_alias = defining_alias
        self.allow_tuple_literal = allow_tuple_literal
        # Positive if we are analyzing arguments of another (outer) type
        self.nesting_level = 0
        # Should we allow unnormalized types like `list[int]`
        # (currently allowed in stubs)?
        self.allow_unnormalized = allow_unnormalized
        # Should we accept unbound type variables (always OK in aliases)?
        self.allow_unbound_tvars = allow_unbound_tvars or defining_alias
        # If false, record incomplete ref if we generate PlaceholderType.
        self.allow_placeholder = allow_placeholder
        # Should we report an error whenever we encounter a RawExpressionType outside
        # of a Literal context: e.g. whenever we encounter an invalid type? Normally,
        # we want to report an error, but the caller may want to do more specialized
        # error handling.
        self.report_invalid_types = report_invalid_types
        self.plugin = plugin
        self.options = options
        self.is_typeshed_stub = is_typeshed_stub
        # Names of type aliases encountered while analysing a type will be collected here.
        self.aliases_used = set()  # type: Set[str]

    def visit_unbound_type(self, t: UnboundType, defining_literal: bool = False) -> Type:
        typ = self.visit_unbound_type_nonoptional(t, defining_literal)
        if t.optional:
            # We don't need to worry about double-wrapping Optionals or
            # wrapping Anys: Union simplification will take care of that.
            return make_optional_type(typ)
        return typ

    def visit_unbound_type_nonoptional(self, t: UnboundType, defining_literal: bool) -> Type:
        sym = self.lookup_qualified(t.name, t)
        if sym is not None:
            node = sym.node
            if isinstance(node, PlaceholderNode):
                if node.becomes_typeinfo:
                    # Reference to placeholder type.
                    if self.api.final_iteration:
                        self.cannot_resolve_type(t)
                        return AnyType(TypeOfAny.from_error)
                    elif self.allow_placeholder:
                        self.api.defer()
                    else:
                        self.api.record_incomplete_ref()
                    return PlaceholderType(node.fullname(), self.anal_array(t.args), t.line)
                else:
                    if self.api.final_iteration:
                        self.cannot_resolve_type(t)
                        return AnyType(TypeOfAny.from_error)
                    else:
                        # Reference to an unknown placeholder node.
                        self.api.record_incomplete_ref()
                        return AnyType(TypeOfAny.special_form)
            if node is None:
                self.fail('Internal error (node is None, kind={})'.format(sym.kind), t)
                return AnyType(TypeOfAny.special_form)
            fullname = node.fullname()
            hook = self.plugin.get_type_analyze_hook(fullname)
            if hook is not None:
                return hook(AnalyzeTypeContext(t, t, self))
            if (fullname in nongen_builtins
                    and t.args and
                    not self.allow_unnormalized):
                self.fail(no_subscript_builtin_alias(fullname,
                                                     propose_alt=not self.defining_alias), t)
            tvar_def = self.tvar_scope.get_binding(sym)
            if isinstance(sym.node, TypeVarExpr) and tvar_def is not None and self.defining_alias:
                self.fail('Can\'t use bound type variable "{}"'
                          ' to define generic alias'.format(t.name), t)
                return AnyType(TypeOfAny.from_error)
            if isinstance(sym.node, TypeVarExpr) and tvar_def is not None:
                if len(t.args) > 0:
                    self.fail('Type variable "{}" used with arguments'.format(t.name), t)
                return TypeVarType(tvar_def, t.line)
            special = self.try_analyze_special_unbound_type(t, fullname)
            if special is not None:
                return special
            if isinstance(node, TypeAlias):
                self.aliases_used.add(fullname)
                all_vars = node.alias_tvars
                target = node.target
                an_args = self.anal_array(t.args)
                disallow_any = self.options.disallow_any_generics and not self.is_typeshed_stub
                res = expand_type_alias(target, all_vars, an_args, self.fail, node.no_args, t,
                                        unexpanded_type=t,
                                        disallow_any=disallow_any)
                # The only case where expand_type_alias() can return an incorrect instance is
                # when it is top-level instance, so no need to recurse.
                if (isinstance(res, Instance) and len(res.args) != len(res.type.type_vars) and
                        not self.defining_alias):
                    fix_instance(res, self.fail, disallow_any=disallow_any, use_generic_error=True,
                                 unexpanded_type=t)
                return res
            elif isinstance(node, TypeInfo):
                return self.analyze_type_with_type_info(node, t.args, t)
            else:
                return self.analyze_unbound_type_without_type_info(t, sym, defining_literal)
        else:  # sym is None
            return AnyType(TypeOfAny.special_form)

    def cannot_resolve_type(self, t: UnboundType) -> None:
        # TODO: Move error message generation to messages.py. We'd first
        #       need access to MessageBuilder here. Also move the similar
        #       message generation logic in semanal.py.
        self.api.fail(
            'Cannot resolve name "{}" (possible cyclic definition)'.format(t.name),
            t)

    def try_analyze_special_unbound_type(self, t: UnboundType, fullname: str) -> Optional[Type]:
        """Bind special type that is recognized through magic name such as 'typing.Any'.

        Return the bound type if successful, and return None if the type is a normal type.
        """
        if fullname == 'builtins.None':
            return NoneType()
        elif fullname == 'typing.Any' or fullname == 'builtins.Any':
            return AnyType(TypeOfAny.explicit)
        elif fullname in ('typing.Final', 'typing_extensions.Final'):
            self.fail("Final can be only used as an outermost qualifier"
                      " in a variable annotation", t)
            return AnyType(TypeOfAny.from_error)
        elif fullname == 'typing.Tuple':
            # Tuple is special because it is involved in builtin import cycle
            # and may be not ready when used.
            sym = self.api.lookup_fully_qualified_or_none('builtins.tuple')
            if not sym or isinstance(sym.node, PlaceholderNode):
                if self.api.is_incomplete_namespace('builtins'):
                    self.api.record_incomplete_ref()
                else:
                    self.fail("Name 'tuple' is not defined", t)
                return AnyType(TypeOfAny.special_form)
            if len(t.args) == 0 and not t.empty_tuple_index:
                # Bare 'Tuple' is same as 'tuple'
                any_type = self.get_omitted_any(t)
                return self.named_type('builtins.tuple', [any_type],
                                       line=t.line, column=t.column)
            if len(t.args) == 2 and isinstance(t.args[1], EllipsisType):
                # Tuple[T, ...] (uniform, variable-length tuple)
                instance = self.named_type('builtins.tuple', [self.anal_type(t.args[0])])
                instance.line = t.line
                return instance
            return self.tuple_type(self.anal_array(t.args))
        elif fullname == 'typing.Union':
            items = self.anal_array(t.args)
            return UnionType.make_union(items)
        elif fullname == 'typing.Optional':
            if len(t.args) != 1:
                self.fail('Optional[...] must have exactly one type argument', t)
                return AnyType(TypeOfAny.from_error)
            item = self.anal_type(t.args[0])
            return make_optional_type(item)
        elif fullname == 'typing.Callable':
            return self.analyze_callable_type(t)
        elif fullname == 'typing.Type':
            if len(t.args) == 0:
                any_type = self.get_omitted_any(t)
                return TypeType(any_type, line=t.line, column=t.column)
            if len(t.args) != 1:
                self.fail('Type[...] must have exactly one type argument', t)
            item = self.anal_type(t.args[0])
            return TypeType.make_normalized(item, line=t.line)
        elif fullname == 'typing.ClassVar':
            if self.nesting_level > 0:
                self.fail('Invalid type: ClassVar nested inside other type', t)
            if len(t.args) == 0:
                return AnyType(TypeOfAny.from_omitted_generics, line=t.line, column=t.column)
            if len(t.args) != 1:
                self.fail('ClassVar[...] must have at most one type argument', t)
                return AnyType(TypeOfAny.from_error)
            item = self.anal_type(t.args[0])
            if isinstance(item, TypeVarType) or get_type_vars(item):
                self.fail('Invalid type: ClassVar cannot be generic', t)
                return AnyType(TypeOfAny.from_error)
            return item
        elif fullname in ('mypy_extensions.NoReturn', 'typing.NoReturn'):
            return UninhabitedType(is_noreturn=True)
        elif fullname in ('typing_extensions.Literal', 'typing.Literal'):
            return self.analyze_literal_type(t)
        elif fullname == 'typing_extensions.Annotated':
            if len(t.args) < 2:
                self.fail("Annotated[...] must have exactly one type argument"
                          " and at least one annotation", t)
                return AnyType(TypeOfAny.from_error)
            return self.anal_type(t.args[0])
        return None

    def get_omitted_any(self, typ: Type, fullname: Optional[str] = None) -> AnyType:
        disallow_any = not self.is_typeshed_stub and self.options.disallow_any_generics
        return get_omitted_any(disallow_any, self.fail, typ, fullname)

    def analyze_type_with_type_info(self, info: TypeInfo, args: List[Type], ctx: Context) -> Type:
        """Bind unbound type when were able to find target TypeInfo.

        This handles simple cases like 'int', 'modname.UserClass[str]', etc.
        """
        if len(args) > 0 and info.fullname() == 'builtins.tuple':
            fallback = Instance(info, [AnyType(TypeOfAny.special_form)], ctx.line)
            return TupleType(self.anal_array(args), fallback, ctx.line)
        # Analyze arguments and (usually) construct Instance type. The
        # number of type arguments and their values are
        # checked only later, since we do not always know the
        # valid count at this point. Thus we may construct an
        # Instance with an invalid number of type arguments.
        instance = Instance(info, self.anal_array(args), ctx.line, ctx.column)
        # Check type argument count.
        if len(instance.args) != len(info.type_vars) and not self.defining_alias:
            fix_instance(instance, self.fail,
                         disallow_any=self.options.disallow_any_generics and
                         not self.is_typeshed_stub)

        tup = info.tuple_type
        if tup is not None:
            # The class has a Tuple[...] base class so it will be
            # represented as a tuple type.
            if args:
                self.fail('Generic tuple types not supported', ctx)
                return AnyType(TypeOfAny.from_error)
            return tup.copy_modified(items=self.anal_array(tup.items),
                                     fallback=instance)
        td = info.typeddict_type
        if td is not None:
            # The class has a TypedDict[...] base class so it will be
            # represented as a typeddict type.
            if args:
                self.fail('Generic TypedDict types not supported', ctx)
                return AnyType(TypeOfAny.from_error)
            # Create a named TypedDictType
            return td.copy_modified(item_types=self.anal_array(list(td.items.values())),
                                    fallback=instance)
        return instance

    def analyze_unbound_type_without_type_info(self, t: UnboundType, sym: SymbolTableNode,
                                               defining_literal: bool) -> Type:
        """Figure out what an unbound type that doesn't refer to a TypeInfo node means.

        This is something unusual. We try our best to find out what it is.
        """
        name = sym.fullname
        if name is None:
            assert sym.node is not None
            name = sym.node.name()
        # Option 1:
        # Something with an Any type -- make it an alias for Any in a type
        # context. This is slightly problematic as it allows using the type 'Any'
        # as a base class -- however, this will fail soon at runtime so the problem
        # is pretty minor.
        if isinstance(sym.node, Var) and isinstance(sym.node.type, AnyType):
            return AnyType(TypeOfAny.from_unimported_type,
                           missing_import_name=sym.node.type.missing_import_name)
        # Option 2:
        # Unbound type variable. Currently these may be still valid,
        # for example when defining a generic type alias.
        unbound_tvar = (isinstance(sym.node, TypeVarExpr) and
                        self.tvar_scope.get_binding(sym) is None)
        if self.allow_unbound_tvars and unbound_tvar:
            return t

        # Option 3:
        # Enum value. Note: we only want to return a LiteralType when
        # we're using this enum value specifically within context of
        # a "Literal[...]" type. So, if `defining_literal` is not set,
        # we bail out early with an error.
        #
        # If, in the distant future, we decide to permit things like
        # `def foo(x: Color.RED) -> None: ...`, we can remove that
        # check entirely.
        if isinstance(sym.node, Var) and sym.node.info and sym.node.info.is_enum:
            value = sym.node.name()
            base_enum_short_name = sym.node.info.name()
            if not defining_literal:
                msg = message_registry.INVALID_TYPE_RAW_ENUM_VALUE.format(
                    base_enum_short_name, value)
                self.fail(msg, t)
                return AnyType(TypeOfAny.from_error)
            return LiteralType(
                value=value,
                fallback=Instance(sym.node.info, [], line=t.line, column=t.column),
                line=t.line,
                column=t.column,
            )

        # None of the above options worked. We parse the args (if there are any)
        # to make sure there are no remaining semanal-only types, then give up.
        t = t.copy_modified(args=self.anal_array(t.args))
        # TODO: Move this message building logic to messages.py.
        notes = []  # type: List[str]
        if isinstance(sym.node, Var):
            # TODO: add a link to alias docs, see #3494.
            message = 'Variable "{}" is not valid as a type'
        elif isinstance(sym.node, (SYMBOL_FUNCBASE_TYPES, Decorator)):
            message = 'Function "{}" is not valid as a type'
            notes.append('Perhaps you need "Callable[...]" or a callback protocol?')
        elif isinstance(sym.node, MypyFile):
            # TODO: suggest a protocol when supported.
            message = 'Module "{}" is not valid as a type'
        elif unbound_tvar:
            message = 'Type variable "{}" is unbound'
            short = name.split('.')[-1]
            notes.append(('(Hint: Use "Generic[{}]" or "Protocol[{}]" base class'
                          ' to bind "{}" inside a class)').format(short, short, short))
            notes.append('(Hint: Use "{}" in function signature to bind "{}"'
                         ' inside a function)'.format(short, short))
        else:
            message = 'Cannot interpret reference "{}" as a type'
        self.fail(message.format(name), t, code=codes.VALID_TYPE)
        for note in notes:
            self.note(note, t)

        # TODO: Would it be better to always return Any instead of UnboundType
        # in case of an error? On one hand, UnboundType has a name so error messages
        # are more detailed, on the other hand, some of them may be bogus,
        # see https://github.com/python/mypy/issues/4987.
        return t

    def visit_any(self, t: AnyType) -> Type:
        return t

    def visit_none_type(self, t: NoneType) -> Type:
        return t

    def visit_uninhabited_type(self, t: UninhabitedType) -> Type:
        return t

    def visit_deleted_type(self, t: DeletedType) -> Type:
        return t

    def visit_type_list(self, t: TypeList) -> Type:
        self.fail('Bracketed expression "[...]" is not valid as a type', t)
        self.note('Did you mean "List[...]"?', t)
        return AnyType(TypeOfAny.from_error)

    def visit_callable_argument(self, t: CallableArgument) -> Type:
        self.fail('Invalid type', t)
        return AnyType(TypeOfAny.from_error)

    def visit_instance(self, t: Instance) -> Type:
        return t

    def visit_type_var(self, t: TypeVarType) -> Type:
        return t

    def visit_callable_type(self, t: CallableType, nested: bool = True) -> Type:
        # Every Callable can bind its own type variables, if they're not in the outer scope
        with self.tvar_scope_frame():
            if self.defining_alias:
                variables = t.variables
            else:
                variables = self.bind_function_type_variables(t, t)
            ret = t.copy_modified(arg_types=self.anal_array(t.arg_types, nested=nested),
                                  ret_type=self.anal_type(t.ret_type, nested=nested),
                                  # If the fallback isn't filled in yet,
                                  # its type will be the falsey FakeInfo
                                  fallback=(t.fallback if t.fallback.type
                                            else self.named_type('builtins.function')),
                                  variables=self.anal_var_defs(variables))
        return ret

    def visit_tuple_type(self, t: TupleType) -> Type:
        # Types such as (t1, t2, ...) only allowed in assignment statements. They'll
        # generate errors elsewhere, and Tuple[t1, t2, ...] must be used instead.
        if t.implicit and not self.allow_tuple_literal:
            self.fail('Syntax error in type annotation', t, code=codes.SYNTAX)
            if len(t.items) == 1:
                self.note('Suggestion: Is there a spurious trailing comma?', t)
            else:
                self.note('Suggestion: Use Tuple[T1, ..., Tn] instead of (T1, ..., Tn)', t)
            return AnyType(TypeOfAny.from_error)
        star_count = sum(1 for item in t.items if isinstance(item, StarType))
        if star_count > 1:
            self.fail('At most one star type allowed in a tuple', t)
            if t.implicit:
                return TupleType([AnyType(TypeOfAny.from_error) for _ in t.items],
                                 self.named_type('builtins.tuple'),
                                 t.line)
            else:
                return AnyType(TypeOfAny.from_error)
        any_type = AnyType(TypeOfAny.special_form)
        # If the fallback isn't filled in yet, its type will be the falsey FakeInfo
        fallback = (t.partial_fallback if t.partial_fallback.type
                    else self.named_type('builtins.tuple', [any_type]))
        return TupleType(self.anal_array(t.items), fallback, t.line)

    def visit_typeddict_type(self, t: TypedDictType) -> Type:
        items = OrderedDict([
            (item_name, self.anal_type(item_type))
            for (item_name, item_type) in t.items.items()
        ])
        return TypedDictType(items, set(t.required_keys), t.fallback)

    def visit_raw_expression_type(self, t: RawExpressionType) -> Type:
        # We should never see a bare Literal. We synthesize these raw literals
        # in the earlier stages of semantic analysis, but those
        # "fake literals" should always be wrapped in an UnboundType
        # corresponding to 'Literal'.
        #
        # Note: if at some point in the distant future, we decide to
        # make signatures like "foo(x: 20) -> None" legal, we can change
        # this method so it generates and returns an actual LiteralType
        # instead.

        if self.report_invalid_types:
            if t.base_type_name in ('builtins.int', 'builtins.bool'):
                # The only time it makes sense to use an int or bool is inside of
                # a literal type.
                msg = "Invalid type: try using Literal[{}] instead?".format(repr(t.literal_value))
            elif t.base_type_name in ('builtins.float', 'builtins.complex'):
                # We special-case warnings for floats and complex numbers.
                msg = "Invalid type: {} literals cannot be used as a type".format(t.simple_name())
            else:
                # And in all other cases, we default to a generic error message.
                # Note: the reason why we use a generic error message for strings
                # but not ints or bools is because whenever we see an out-of-place
                # string, it's unclear if the user meant to construct a literal type
                # or just misspelled a regular type. So we avoid guessing.
                msg = 'Invalid type comment or annotation'

            self.fail(msg, t, code=codes.VALID_TYPE)
            if t.note is not None:
                self.note(t.note, t)

        return AnyType(TypeOfAny.from_error, line=t.line, column=t.column)

    def visit_literal_type(self, t: LiteralType) -> Type:
        return t

    def visit_star_type(self, t: StarType) -> Type:
        return StarType(self.anal_type(t.type), t.line)

    def visit_union_type(self, t: UnionType) -> Type:
        return UnionType(self.anal_array(t.items), t.line)

    def visit_partial_type(self, t: PartialType) -> Type:
        assert False, "Internal error: Unexpected partial type"

    def visit_ellipsis_type(self, t: EllipsisType) -> Type:
        self.fail("Unexpected '...'", t)
        return AnyType(TypeOfAny.from_error)

    def visit_type_type(self, t: TypeType) -> Type:
        return TypeType.make_normalized(self.anal_type(t.item), line=t.line)

    def visit_placeholder_type(self, t: PlaceholderType) -> Type:
        n = None if t.fullname is None else self.api.lookup_fully_qualified(t.fullname)
        if not n or isinstance(n.node, PlaceholderNode):
            self.api.defer()  # Still incomplete
            return t
        else:
            # TODO: Handle non-TypeInfo
            assert isinstance(n.node, TypeInfo)
            return self.analyze_type_with_type_info(n.node, t.args, t)

    def analyze_callable_type(self, t: UnboundType) -> Type:
        fallback = self.named_type('builtins.function')
        if len(t.args) == 0:
            # Callable (bare). Treat as Callable[..., Any].
            any_type = self.get_omitted_any(t)
            ret = CallableType([any_type, any_type],
                               [nodes.ARG_STAR, nodes.ARG_STAR2],
                               [None, None],
                               ret_type=any_type,
                               fallback=fallback,
                               is_ellipsis_args=True)
        elif len(t.args) == 2:
            ret_type = t.args[1]
            if isinstance(t.args[0], TypeList):
                # Callable[[ARG, ...], RET] (ordinary callable type)
                analyzed_args = self.analyze_callable_args(t.args[0])
                if analyzed_args is None:
                    return AnyType(TypeOfAny.from_error)
                args, kinds, names = analyzed_args
                ret = CallableType(args,
                                   kinds,
                                   names,
                                   ret_type=ret_type,
                                   fallback=fallback)
            elif isinstance(t.args[0], EllipsisType):
                # Callable[..., RET] (with literal ellipsis; accept arbitrary arguments)
                ret = CallableType([AnyType(TypeOfAny.explicit),
                                    AnyType(TypeOfAny.explicit)],
                                   [nodes.ARG_STAR, nodes.ARG_STAR2],
                                   [None, None],
                                   ret_type=ret_type,
                                   fallback=fallback,
                                   is_ellipsis_args=True)
            else:
                self.fail('The first argument to Callable must be a list of types or "..."', t)
                return AnyType(TypeOfAny.from_error)
        else:
            self.fail('Please use "Callable[[<parameters>], <return type>]" or "Callable"', t)
            return AnyType(TypeOfAny.from_error)
        assert isinstance(ret, CallableType)
        return ret.accept(self)

    def analyze_callable_args(self, arglist: TypeList) -> Optional[Tuple[List[Type],
                                                                         List[int],
                                                                         List[Optional[str]]]]:
        args = []   # type: List[Type]
        kinds = []  # type: List[int]
        names = []  # type: List[Optional[str]]
        for arg in arglist.items:
            if isinstance(arg, CallableArgument):
                args.append(arg.typ)
                names.append(arg.name)
                if arg.constructor is None:
                    return None
                found = self.lookup_qualified(arg.constructor, arg)
                if found is None:
                    # Looking it up already put an error message in
                    return None
                elif found.fullname not in ARG_KINDS_BY_CONSTRUCTOR:
                    self.fail('Invalid argument constructor "{}"'.format(
                        found.fullname), arg)
                    return None
                else:
                    assert found.fullname is not None
                    kind = ARG_KINDS_BY_CONSTRUCTOR[found.fullname]
                    kinds.append(kind)
                    if arg.name is not None and kind in {ARG_STAR, ARG_STAR2}:
                        self.fail("{} arguments should not have names".format(
                            arg.constructor), arg)
                        return None
            else:
                args.append(arg)
                kinds.append(ARG_POS)
                names.append(None)
        # Note that arglist below is only used for error context.
        check_arg_names(names, [arglist] * len(args), self.fail, "Callable")
        check_arg_kinds(kinds, [arglist] * len(args), self.fail)
        return args, kinds, names

    def analyze_literal_type(self, t: UnboundType) -> Type:
        if len(t.args) == 0:
            self.fail('Literal[...] must have at least one parameter', t)
            return AnyType(TypeOfAny.from_error)

        output = []  # type: List[Type]
        for i, arg in enumerate(t.args):
            analyzed_types = self.analyze_literal_param(i + 1, arg, t)
            if analyzed_types is None:
                return AnyType(TypeOfAny.from_error)
            else:
                output.extend(analyzed_types)
        return UnionType.make_union(output, line=t.line)

    def analyze_literal_param(self, idx: int, arg: Type, ctx: Context) -> Optional[List[Type]]:
        # This UnboundType was originally defined as a string.
        if isinstance(arg, UnboundType) and arg.original_str_expr is not None:
            assert arg.original_str_fallback is not None
            return [LiteralType(
                value=arg.original_str_expr,
                fallback=self.named_type_with_normalized_str(arg.original_str_fallback),
                line=arg.line,
                column=arg.column,
            )]

        # If arg is an UnboundType that was *not* originally defined as
        # a string, try expanding it in case it's a type alias or something.
        if isinstance(arg, UnboundType):
            self.nesting_level += 1
            try:
                arg = self.visit_unbound_type(arg, defining_literal=True)
            finally:
                self.nesting_level -= 1

        # Literal[...] cannot contain Any. Give up and add an error message
        # (if we haven't already).
        if isinstance(arg, AnyType):
            # Note: We can encounter Literals containing 'Any' under three circumstances:
            #
            # 1. If the user attempts use an explicit Any as a parameter
            # 2. If the user is trying to use an enum value imported from a module with
            #    no type hints, giving it an an implicit type of 'Any'
            # 3. If there's some other underlying problem with the parameter.
            #
            # We report an error in only the first two cases. In the third case, we assume
            # some other region of the code has already reported a more relevant error.
            #
            # TODO: Once we start adding support for enums, make sure we report a custom
            # error for case 2 as well.
            if arg.type_of_any not in (TypeOfAny.from_error, TypeOfAny.special_form):
                self.fail('Parameter {} of Literal[...] cannot be of type "Any"'.format(idx), ctx)
            return None
        elif isinstance(arg, RawExpressionType):
            # A raw literal. Convert it directly into a literal if we can.
            if arg.literal_value is None:
                name = arg.simple_name()
                if name in ('float', 'complex'):
                    msg = 'Parameter {} of Literal[...] cannot be of type "{}"'.format(idx, name)
                else:
                    msg = 'Invalid type: Literal[...] cannot contain arbitrary expressions'
                self.fail(msg, ctx)
                # Note: we deliberately ignore arg.note here: the extra info might normally be
                # helpful, but it generally won't make sense in the context of a Literal[...].
                return None

            # Remap bytes and unicode into the appropriate type for the correct Python version
            fallback = self.named_type_with_normalized_str(arg.base_type_name)
            assert isinstance(fallback, Instance)
            return [LiteralType(arg.literal_value, fallback, line=arg.line, column=arg.column)]
        elif isinstance(arg, (NoneType, LiteralType)):
            # Types that we can just add directly to the literal/potential union of literals.
            return [arg]
        elif isinstance(arg, Instance) and arg.last_known_value is not None:
            # Types generated from declarations like "var: Final = 4".
            return [arg.last_known_value]
        elif isinstance(arg, UnionType):
            out = []
            for union_arg in arg.items:
                union_result = self.analyze_literal_param(idx, union_arg, ctx)
                if union_result is None:
                    return None
                out.extend(union_result)
            return out
        else:
            self.fail('Parameter {} of Literal[...] is invalid'.format(idx), ctx)
            return None

    def analyze_type(self, t: Type) -> Type:
        return t.accept(self)

    def fail(self, msg: str, ctx: Context, *, code: Optional[ErrorCode] = None) -> None:
        self.fail_func(msg, ctx, code=code)

    def note(self, msg: str, ctx: Context) -> None:
        self.note_func(msg, ctx)

    @contextmanager
    def tvar_scope_frame(self) -> Iterator[None]:
        old_scope = self.tvar_scope
        self.tvar_scope = self.tvar_scope.method_frame()
        yield
        self.tvar_scope = old_scope

    def infer_type_variables(self,
                             type: CallableType) -> List[Tuple[str, TypeVarExpr]]:
        """Return list of unique type variables referred to in a callable."""
        names = []  # type: List[str]
        tvars = []  # type: List[TypeVarExpr]
        for arg in type.arg_types:
            for name, tvar_expr in arg.accept(TypeVariableQuery(self.lookup_qualified,
                                                                self.tvar_scope)):
                if name not in names:
                    names.append(name)
                    tvars.append(tvar_expr)
        # When finding type variables in the return type of a function, don't
        # look inside Callable types.  Type variables only appearing in
        # functions in the return type belong to those functions, not the
        # function we're currently analyzing.
        for name, tvar_expr in type.ret_type.accept(
                TypeVariableQuery(self.lookup_qualified, self.tvar_scope,
                                  include_callables=False)):
            if name not in names:
                names.append(name)
                tvars.append(tvar_expr)
        return list(zip(names, tvars))

    def bind_function_type_variables(self,
                                     fun_type: CallableType, defn: Context) -> List[TypeVarDef]:
        """Find the type variables of the function type and bind them in our tvar_scope"""
        if fun_type.variables:
            for var in fun_type.variables:
                var_node = self.lookup_qualified(var.name, defn)
                assert var_node, "Binding for function type variable not found within function"
                var_expr = var_node.node
                assert isinstance(var_expr, TypeVarExpr)
                self.tvar_scope.bind_new(var.name, var_expr)
            return fun_type.variables
        typevars = self.infer_type_variables(fun_type)
        # Do not define a new type variable if already defined in scope.
        typevars = [(name, tvar) for name, tvar in typevars
                    if not self.is_defined_type_var(name, defn)]
        defs = []  # type: List[TypeVarDef]
        for name, tvar in typevars:
            if not self.tvar_scope.allow_binding(tvar.fullname()):
                self.fail("Type variable '{}' is bound by an outer class".format(name), defn)
            self.tvar_scope.bind_new(name, tvar)
            binding = self.tvar_scope.get_binding(tvar.fullname())
            assert binding is not None
            defs.append(binding)

        return defs

    def is_defined_type_var(self, tvar: str, context: Context) -> bool:
        tvar_node = self.lookup_qualified(tvar, context)
        if not tvar_node:
            return False
        return self.tvar_scope.get_binding(tvar_node) is not None

    def anal_array(self, a: List[Type], nested: bool = True) -> List[Type]:
        res = []  # type: List[Type]
        for t in a:
            res.append(self.anal_type(t, nested))
        return res

    def anal_type(self, t: Type, nested: bool = True) -> Type:
        if nested:
            self.nesting_level += 1
        try:
            return t.accept(self)
        finally:
            if nested:
                self.nesting_level -= 1

    def anal_var_defs(self, var_defs: List[TypeVarDef]) -> List[TypeVarDef]:
        a = []  # type: List[TypeVarDef]
        for vd in var_defs:
            a.append(TypeVarDef(vd.name,
                                vd.fullname,
                                vd.id.raw_id,
                                self.anal_array(vd.values),
                                vd.upper_bound.accept(self),
                                vd.variance,
                                vd.line))
        return a

    def named_type_with_normalized_str(self, fully_qualified_name: str) -> Instance:
        """Does almost the same thing as `named_type`, except that we immediately
        unalias `builtins.bytes` and `builtins.unicode` to `builtins.str` as appropriate.
        """
        python_version = self.options.python_version
        if python_version[0] == 2 and fully_qualified_name == 'builtins.bytes':
            fully_qualified_name = 'builtins.str'
        if python_version[0] >= 3 and fully_qualified_name == 'builtins.unicode':
            fully_qualified_name = 'builtins.str'
        return self.named_type(fully_qualified_name)

    def named_type(self, fully_qualified_name: str,
                   args: Optional[List[Type]] = None,
                   line: int = -1,
                   column: int = -1) -> Instance:
        node = self.lookup_fqn_func(fully_qualified_name)
        assert isinstance(node.node, TypeInfo)
        any_type = AnyType(TypeOfAny.special_form)
        return Instance(node.node, args or [any_type] * len(node.node.defn.type_vars),
                        line=line, column=column)

    def tuple_type(self, items: List[Type]) -> TupleType:
        any_type = AnyType(TypeOfAny.special_form)
        return TupleType(items, fallback=self.named_type('builtins.tuple', [any_type]))


TypeVarList = List[Tuple[str, TypeVarExpr]]

# Mypyc doesn't support callback protocols yet.
FailCallback = Callable[[str, Context, DefaultNamedArg(Optional[ErrorCode], 'code')], None]


def get_omitted_any(disallow_any: bool, fail: FailCallback,
                    typ: Type, fullname: Optional[str] = None,
                    unexpanded_type: Optional[Type] = None) -> AnyType:
    if disallow_any:
        if fullname in nongen_builtins:
            # We use a dedicated error message for builtin generics (as the most common case).
            alternative = nongen_builtins[fullname]
            fail(message_registry.IMPLICIT_GENERIC_ANY_BUILTIN.format(alternative), typ,
                 code=codes.TYPE_ARG)
        else:
            typ = unexpanded_type or typ
            type_str = typ.name if isinstance(typ, UnboundType) else format_type_bare(typ)

            fail(message_registry.BARE_GENERIC.format(quote_type_string(type_str)), typ,
                 code=codes.TYPE_ARG)
        any_type = AnyType(TypeOfAny.from_error, line=typ.line, column=typ.column)
    else:
        any_type = AnyType(TypeOfAny.from_omitted_generics, line=typ.line, column=typ.column)
    return any_type


def fix_instance(t: Instance, fail: FailCallback,
                 disallow_any: bool, use_generic_error: bool = False,
                 unexpanded_type: Optional[Type] = None) -> None:
    """Fix a malformed instance by replacing all type arguments with Any.

    Also emit a suitable error if this is not due to implicit Any's.
    """
    if len(t.args) == 0:
        if use_generic_error:
            fullname = None  # type: Optional[str]
        else:
            fullname = t.type.fullname()
        any_type = get_omitted_any(disallow_any, fail, t, fullname, unexpanded_type)
        t.args = [any_type] * len(t.type.type_vars)
        return
    # Invalid number of type parameters.
    n = len(t.type.type_vars)
    s = '{} type arguments'.format(n)
    if n == 0:
        s = 'no type arguments'
    elif n == 1:
        s = '1 type argument'
    act = str(len(t.args))
    if act == '0':
        act = 'none'
    fail('"{}" expects {}, but {} given'.format(
        t.type.name(), s, act), t, code=codes.TYPE_ARG)
    # Construct the correct number of type arguments, as
    # otherwise the type checker may crash as it expects
    # things to be right.
    t.args = [AnyType(TypeOfAny.from_error) for _ in t.type.type_vars]
    t.invalid = True


def expand_type_alias(target: Type, alias_tvars: List[str], args: List[Type],
                      fail: FailCallback, no_args: bool, ctx: Context, *,
                      unexpanded_type: Optional[Type] = None,
                      disallow_any: bool = False) -> Type:
    """Expand a (generic) type alias target following the rules outlined in TypeAlias docstring.

    Here:
        target: original target type (contains unbound type variables)
        alias_tvars: type variable names
        args: types to be substituted in place of type variables
        fail: error reporter callback
        no_args: whether original definition used a bare generic `A = List`
        ctx: context where expansion happens
    """
    exp_len = len(alias_tvars)
    act_len = len(args)
    if exp_len > 0 and act_len == 0:
        # Interpret bare Alias same as normal generic, i.e., Alias[Any, Any, ...]
        assert alias_tvars is not None
        return set_any_tvars(target, alias_tvars, ctx.line, ctx.column,
                             disallow_any=disallow_any, fail=fail,
                             unexpanded_type=unexpanded_type)
    if exp_len == 0 and act_len == 0:
        if no_args:
            assert isinstance(target, Instance)
            return Instance(target.type, [], line=ctx.line, column=ctx.column)
        return target
    if exp_len == 0 and act_len > 0 and isinstance(target, Instance) and no_args:
        tp = Instance(target.type, args)
        tp.line = ctx.line
        tp.column = ctx.column
        return tp
    if act_len != exp_len:
        fail('Bad number of arguments for type alias, expected: %s, given: %s'
             % (exp_len, act_len), ctx)
        return set_any_tvars(target, alias_tvars or [],
                             ctx.line, ctx.column, from_error=True)
    typ = replace_alias_tvars(target, alias_tvars, args, ctx.line, ctx.column)
    # HACK: Implement FlexibleAlias[T, typ] by expanding it to typ here.
    if (isinstance(typ, Instance)
            and typ.type.fullname() == 'mypy_extensions.FlexibleAlias'):
        typ = typ.args[-1]
    return typ


def replace_alias_tvars(tp: Type, vars: List[str], subs: List[Type],
                        newline: int, newcolumn: int) -> Type:
    """Replace type variables in a generic type alias tp with substitutions subs
    resetting context. Length of subs should be already checked.
    """
    typ_args = get_typ_args(tp)
    new_args = typ_args[:]
    for i, arg in enumerate(typ_args):
        if isinstance(arg, (UnboundType, TypeVarType)):
            tvar = arg.name  # type: Optional[str]
        else:
            tvar = None
        if tvar and tvar in vars:
            # Perform actual substitution...
            new_args[i] = subs[vars.index(tvar)]
        else:
            # ...recursively, if needed.
            new_args[i] = replace_alias_tvars(arg, vars, subs, newline, newcolumn)
    return set_typ_args(tp, new_args, newline, newcolumn)


def set_any_tvars(tp: Type, vars: List[str],
                  newline: int, newcolumn: int, *,
                  from_error: bool = False,
                  disallow_any: bool = False,
                  fail: Optional[FailCallback] = None,
                  unexpanded_type: Optional[Type] = None) -> Type:
    if from_error or disallow_any:
        type_of_any = TypeOfAny.from_error
    else:
        type_of_any = TypeOfAny.from_omitted_generics
    if disallow_any:
        assert fail is not None
        otype = unexpanded_type or tp
        type_str = otype.name if isinstance(otype, UnboundType) else format_type_bare(otype)

        fail(message_registry.BARE_GENERIC.format(quote_type_string(type_str)),
             Context(newline, newcolumn), code=codes.TYPE_ARG)
    any_type = AnyType(type_of_any, line=newline, column=newcolumn)
    return replace_alias_tvars(tp, vars, [any_type] * len(vars), newline, newcolumn)


def remove_dups(tvars: Iterable[T]) -> List[T]:
    # Get unique elements in order of appearance
    all_tvars = set()  # type: Set[T]
    new_tvars = []  # type: List[T]
    for t in tvars:
        if t not in all_tvars:
            new_tvars.append(t)
            all_tvars.add(t)
    return new_tvars


def flatten_tvars(ll: Iterable[List[T]]) -> List[T]:
    return remove_dups(chain.from_iterable(ll))


class TypeVariableQuery(TypeQuery[TypeVarList]):

    def __init__(self,
                 lookup: Callable[[str, Context], Optional[SymbolTableNode]],
                 scope: 'TypeVarScope',
                 *,
                 include_callables: bool = True,
                 include_bound_tvars: bool = False) -> None:
        self.include_callables = include_callables
        self.lookup = lookup
        self.scope = scope
        self.include_bound_tvars = include_bound_tvars
        super().__init__(flatten_tvars)

    def _seems_like_callable(self, type: UnboundType) -> bool:
        if not type.args:
            return False
        if isinstance(type.args[0], (EllipsisType, TypeList)):
            return True
        return False

    def visit_unbound_type(self, t: UnboundType) -> TypeVarList:
        name = t.name
        node = self.lookup(name, t)
        if node and isinstance(node.node, TypeVarExpr) and (
                self.include_bound_tvars or self.scope.get_binding(node) is None):
            assert isinstance(node.node, TypeVarExpr)
            return [(name, node.node)]
        elif not self.include_callables and self._seems_like_callable(t):
            return []
        elif node and node.fullname in ('typing_extensions.Literal', 'typing.Literal'):
            return []
        else:
            return super().visit_unbound_type(t)

    def visit_callable_type(self, t: CallableType) -> TypeVarList:
        if self.include_callables:
            return super().visit_callable_type(t)
        else:
            return []


def check_for_explicit_any(typ: Optional[Type],
                           options: Options,
                           is_typeshed_stub: bool,
                           msg: MessageBuilder,
                           context: Context) -> None:
    if (options.disallow_any_explicit and
            not is_typeshed_stub and
            typ and
            has_explicit_any(typ)):
        msg.explicit_any(context)


def has_explicit_any(t: Type) -> bool:
    """
    Whether this type is or type it contains is an Any coming from explicit type annotation
    """
    return t.accept(HasExplicitAny())


class HasExplicitAny(TypeQuery[bool]):
    def __init__(self) -> None:
        super().__init__(any)

    def visit_any(self, t: AnyType) -> bool:
        return t.type_of_any == TypeOfAny.explicit

    def visit_typeddict_type(self, t: TypedDictType) -> bool:
        # typeddict is checked during TypedDict declaration, so don't typecheck it here.
        return False


def has_any_from_unimported_type(t: Type) -> bool:
    """Return true if this type is Any because an import was not followed.

    If type t is such Any type or has type arguments that contain such Any type
    this function will return true.
    """
    return t.accept(HasAnyFromUnimportedType())


class HasAnyFromUnimportedType(TypeQuery[bool]):
    def __init__(self) -> None:
        super().__init__(any)

    def visit_any(self, t: AnyType) -> bool:
        return t.type_of_any == TypeOfAny.from_unimported_type

    def visit_typeddict_type(self, t: TypedDictType) -> bool:
        # typeddict is checked during TypedDict declaration, so don't typecheck it here
        return False


def collect_any_types(t: Type) -> List[AnyType]:
    """Return all inner `AnyType`s of type t"""
    return t.accept(CollectAnyTypesQuery())


class CollectAnyTypesQuery(TypeQuery[List[AnyType]]):
    def __init__(self) -> None:
        super().__init__(self.combine_lists_strategy)

    def visit_any(self, t: AnyType) -> List[AnyType]:
        return [t]

    @classmethod
    def combine_lists_strategy(cls, it: Iterable[List[AnyType]]) -> List[AnyType]:
        result = []  # type: List[AnyType]
        for l in it:
            result.extend(l)
        return result


def collect_all_inner_types(t: Type) -> List[Type]:
    """
    Return all types that `t` contains
    """
    return t.accept(CollectAllInnerTypesQuery())


class CollectAllInnerTypesQuery(TypeQuery[List[Type]]):
    def __init__(self) -> None:
        super().__init__(self.combine_lists_strategy)

    def query_types(self, types: Iterable[Type]) -> List[Type]:
        return self.strategy([t.accept(self) for t in types]) + list(types)

    @classmethod
    def combine_lists_strategy(cls, it: Iterable[List[Type]]) -> List[Type]:
        return list(itertools.chain.from_iterable(it))


def make_optional_type(t: Type) -> Type:
    """Return the type corresponding to Optional[t].

    Note that we can't use normal union simplification, since this function
    is called during semantic analysis and simplification only works during
    type checking.
    """
    if isinstance(t, NoneType):
        return t
    elif isinstance(t, UnionType):
        items = [item for item in union_items(t)
                 if not isinstance(item, NoneType)]
        return UnionType(items + [NoneType()], t.line, t.column)
    else:
        return UnionType([t, NoneType()], t.line, t.column)


def fix_instance_types(t: Type, fail: FailCallback) -> None:
    """Recursively fix all instance types (type argument count) in a given type.

    For example 'Union[Dict, List[str, int]]' will be transformed into
    'Union[Dict[Any, Any], List[Any]]' in place.
    """
    t.accept(InstanceFixer(fail))


class InstanceFixer(TypeTraverserVisitor):
    def __init__(self, fail: FailCallback) -> None:
        self.fail = fail

    def visit_instance(self, typ: Instance) -> None:
        super().visit_instance(typ)
        if len(typ.args) != len(typ.type.type_vars):
            fix_instance(typ, self.fail, disallow_any=False, use_generic_error=True)
