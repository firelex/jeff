using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class ContractTests
{
    private static JsonNode? J(string json) => JsonAssert.Parse(json);

    [Fact]
    public void EverySchemaFileIsListedAndLoadsWithItsId()
    {
        var files = Directory.EnumerateFiles(Repo.PathOf("contract", "schemas"))
            .Select(file => Path.GetFileName(file).Replace(".schema.json", "", StringComparison.Ordinal)).OrderBy(name => name, StringComparer.Ordinal);
        Assert.Equal(files, Contract.Schemas.OrderBy(name => name, StringComparer.Ordinal));
        foreach (var name in Contract.Schemas)
        {
            Assert.Equal($"https://jeff.dev/schemas/{name}.schema.json", Contract.LoadSchema(name)["$id"]!.GetValue<string>());
        }
    }

    [Fact]
    public void ValidateListsEveryProblemAsPythonDoes()
    {
        var error = Assert.Throws<ContractError>(() => Contract.Validate("decision", J("{\"value\": 1}"), "a decision"));
        // the exact text jeff.contract.validate("decision", {"value": 1}, "a decision") raises
        Assert.Equal("a decision does not match the decision schema:\n  at /: 'confidence' is a required property\n"
            + "  at /: 'abstained' is a required property\n  at /: 'calibrated' is a required property\n"
            + "  at /: 'interface' is a required property\n  at /: 'function' is a required property\n"
            + "  at /: 'version' is a required property\n  at /: 'probabilities' is a required property\n"
            + "  at /: 'score' is a required property\n  at /: 'route' is a required property\n"
            + "  at /: 'decided_by' is a required property\n  at /: 'escalations' is a required property\n"
            + "  at /: 'timing' is a required property\n  at /: 'evidence_refs' is a required property", error.Message);
    }

    [Fact]
    public void OneDefinitionOfASchemaCanBeCheckedAcrossSchemas()
    {
        Assert.True(Contract.IsValid("socket", J("{\"op\": \"predict\", \"id\": 1, \"adapter\": \"a\", \"rows\": [{}]}"), "predict"));
        Assert.False(Contract.IsValid("socket", J("{\"op\": \"predict\", \"id\": 1, \"adapter\": \"a\", \"rows\": []}"), "predict"));
        Assert.True(Contract.IsValid("decide_response", J("{\"decisions\": []}")));
        Assert.Equal("[] should be non-empty (at /rows)",
            Contract.FirstProblem("socket", J("{\"op\": \"predict\", \"id\": 1, \"adapter\": \"a\", \"rows\": []}"), "predict"));
        Assert.Contains("no definition 'nope'", Assert.Throws<ContractError>(() => Contract.FirstProblem("socket", J("{}"), "nope")).Message);
    }

    // Each expected line is what jeff.contract.first_problem gives for the same document (Python 3.13, jsonschema 4.26).
    [Theory]
    [InlineData("socket", "{\"op\": \"predict\", \"id\": 4, \"adapter\": \"a\"}", null, "'rows' is a required property")]
    [InlineData("socket", "{\"op\": \"stats\", \"id\": 4, \"extra\": 1}", null, "Additional properties are not allowed ('extra' was unexpected)")]
    [InlineData("socket", "{\"op\": \"nope\", \"id\": 3}", null, "'nope' is not one of ['installed', 'reload', 'stats', 'health'] (at /op)")]
    [InlineData("socket", "{\"op\": \"call\", \"id\": 5, \"handle\": 1}", "call", "'input' is a required property")]
    [InlineData("decide_request", "{\"calls\": [{\"function\": \"f\", \"input\": \"x\"}], \"on_low_confidence\": \"maybe\"}", null,
        "'maybe' is not one of ['return', 'error'] (at /on_low_confidence)")]
    [InlineData("decide_request",
        "{\"calls\": [{\"function\": \"f\", \"input\": \"x\", \"labels\": 5}, {\"function\": \"f\", \"input\": \"x\", \"labels\": 5}, {\"function\": \"f\", \"input\": \"x\", \"labels\": 5}]}",
        null, "5 is not of type 'object' (at /calls/0/labels)")]
    public void FirstProblemNamesTheFieldThatIsWrongAsPythonDoes(string schema, string document, string? definition, string expected)
        => Assert.Equal(expected, Contract.FirstProblem(schema, J(document), definition));

    [Fact]
    public void FirstProblemReportsTooManyItemsByTheirCount()
    {
        var calls = new JsonArray(Enumerable.Range(0, 300).Select(_ => (JsonNode?)J("{\"function\": \"f\", \"input\": \"x\"}")).ToArray());
        Assert.Equal("300 items, at most 256 are allowed (at /calls)", Contract.FirstProblem("decide_request", new JsonObject { ["calls"] = calls }));
    }

    [Fact]
    public void FirstProblemRefusesADocumentThatMatches()
    {
        Assert.Throws<ContractError>(() => Contract.FirstProblem("decide_response", J("{\"decisions\": []}")));
    }

    [Fact]
    public void TheInterfacesHaveTheirQuestionTypesAndValues()
    {
        var found = Contract.Interfaces();
        Assert.Equal(QuestionType.Noul, found["detect"].QuestionType);
        Assert.Equal(new[] { "match", "possible_match", "no_match" }, found["equivalent"].Values);
        Assert.Null(found["classify"].Values);
    }

    [Fact]
    public void LoadingTheSchemasChecksThatTheValidatorKnowsEveryKeyword()
    {
        // Contract loads every schema at once and throws a ContractError naming any keyword SchemaRegistry does not know.
        foreach (var name in Contract.Schemas) Assert.NotNull(Contract.LoadSchema(name));
        var unknown = Assert.Throws<ContractError>(() => new SchemaRegistry([]).Errors(null, J("{\"exclusiveMaximum\": 1}")!, "x").ToList());
        Assert.Contains("'exclusiveMaximum'", unknown.Message);
    }

    // Each row is what jeff.contract.first_problem and jeff.contract.validate(..., "the doc", definition) give for the
    // same document (Python 3.13, jsonschema 4.26); null: the document matches. Recorded by running the Python SDK.
    [Theory]
    [InlineData("graph", "{\"graph\": \"g\", \"version\": \"1.0.0\", \"input\": \"text\", \"params\": [], \"nodes\": {\"a\": {\"call\": \"spam.is_spam\", \"code\": \"x\"}}, \"outputs\": [\"a\"]}", null, "{'call': 'spam.is_spam', 'code': 'x'} is valid under each of {'required': ['code']}, {'required': ['call']} (at /nodes/a)", "the doc does not match the graph schema:\n  at /nodes/a: {'call': 'spam.is_spam', 'code': 'x'} is valid under each of {'required': ['code']}, {'required': ['call']}")]
    [InlineData("graph", "{\"graph\": \"g\", \"version\": \"1.0.0\", \"input\": \"text\", \"params\": [], \"nodes\": {\"a\": {}}, \"outputs\": [\"a\"]}", null, "'call' is a required property (at /nodes/a)", "the doc does not match the graph schema:\n  at /nodes/a: {} is not valid under any of the given schemas\n    - 'call' is a required property\n    - 'code' is a required property")]
    [InlineData("graph", "{\"graph\": \"g\", \"version\": \"1.0.0\", \"input\": \"text\", \"params\": [], \"nodes\": {\"a\": {\"call\": \"spam.is_spam\", \"when\": {\"node\": \"b\", \"is\": 1.5}}}, \"outputs\": [\"a\"]}", null, "1.5 is not of type 'string', 'boolean', 'integer' (at /nodes/a/when/is)", "the doc does not match the graph schema:\n  at /nodes/a/when: {'node': 'b', 'is': 1.5} is not valid under any of the given schemas\n    - Additional properties are not allowed ('is', 'node' were unexpected)\n    - 'all' is a required property\n    - Additional properties are not allowed ('is', 'node' were unexpected)\n    - 'any' is a required property\n    - Additional properties are not allowed ('is', 'node' were unexpected)\n    - 'not' is a required property\n    - 1.5 is not of type 'string', 'boolean', 'integer'\n    - Additional properties are not allowed ('is' was unexpected)\n    - 'in' is a required property\n    - Additional properties are not allowed ('is' was unexpected)\n    - 'abstained' is a required property\n    - Additional properties are not allowed ('is' was unexpected)\n    - 'confidence_below' is a required property")]
    [InlineData("graph", "{\"graph\": \"g\", \"version\": \"1.0.0\", \"input\": \"text\", \"params\": [], \"nodes\": {\"a\": {\"call\": \"spam.is_spam\", \"when\": {\"not\": {\"all\": []}}}}, \"outputs\": [\"a\"]}", null, "[] should be non-empty (at /nodes/a/when/not/all)", "the doc does not match the graph schema:\n  at /nodes/a/when: {'not': {'all': []}} is not valid under any of the given schemas\n    - Additional properties are not allowed ('not' was unexpected)\n    - 'all' is a required property\n    - Additional properties are not allowed ('not' was unexpected)\n    - 'any' is a required property\n    - {'all': []} is not valid under any of the given schemas\n    - Additional properties are not allowed ('not' was unexpected)\n    - 'node' is a required property\n    - 'is' is a required property\n    - Additional properties are not allowed ('not' was unexpected)\n    - 'node' is a required property\n    - 'in' is a required property\n    - Additional properties are not allowed ('not' was unexpected)\n    - 'node' is a required property\n    - 'abstained' is a required property\n    - Additional properties are not allowed ('not' was unexpected)\n    - 'node' is a required property\n    - 'confidence_below' is a required property")]
    [InlineData("graph", "{\"graph\": \"Bad Name\", \"version\": \"1.0\", \"input\": \"text\", \"params\": [], \"nodes\": {\"a\": {\"call\": \"spam.is_spam\"}}, \"outputs\": [\"a\"], \"extra\": 1, \"zzz\": 2}", null, "Additional properties are not allowed ('extra', 'zzz' were unexpected)", "the doc does not match the graph schema:\n  at /: Additional properties are not allowed ('extra', 'zzz' were unexpected)\n  at /graph: 'Bad Name' does not match '^jeff(\\\\.[a-z0-9][a-z0-9_-]*){2}$|^[a-z][a-z0-9_-]*$'\n  at /version: '1.0' does not match '^\\\\d+\\\\.\\\\d+\\\\.\\\\d+$'")]
    [InlineData("graph", "{\"graph\": \"g\", \"version\": \"1.0.0\", \"input\": \"text\", \"params\": [], \"nodes\": {\"a\": {\"call\": \"spam.is_spam\", \"context\": {\"x\": {\"param\": 1}, \"y\": \"z\"}, \"labels\": {\"param\": \"p\", \"q\": 1}}}, \"outputs\": [\"a\"]}", null, "1 is not of type 'string' (at /nodes/a/context/x/param)", "the doc does not match the graph schema:\n  at /nodes/a/context/x/param: 1 is not of type 'string'\n  at /nodes/a/context/y: 'z' is not of type 'object'\n  at /nodes/a/labels: Additional properties are not allowed ('q' was unexpected)")]
    [InlineData("decision", "{\"value\": 1}", null, "'confidence' is a required property", "the doc does not match the decision schema:\n  at /: 'confidence' is a required property\n  at /: 'abstained' is a required property\n  at /: 'calibrated' is a required property\n  at /: 'interface' is a required property\n  at /: 'function' is a required property\n  at /: 'version' is a required property\n  at /: 'probabilities' is a required property\n  at /: 'score' is a required property\n  at /: 'route' is a required property\n  at /: 'decided_by' is a required property\n  at /: 'escalations' is a required property\n  at /: 'timing' is a required property\n  at /: 'evidence_refs' is a required property")]
    [InlineData("socket", "{\"op\": \"predict\", \"id\": 0, \"adapter\": \"\", \"rows\": [1, \"x\"], \"deadline_ms\": 0}", "predict", "'' should be non-empty (at /adapter)", "the doc does not match predict in the socket schema:\n  at /adapter: '' should be non-empty\n  at /deadline_ms: 0 is less than or equal to the minimum of 0\n  at /id: 0 is less than the minimum of 1\n  at /rows/0: 1 is not of type 'object'\n  at /rows/1: 'x' is not of type 'object'")]
    [InlineData("socket", "{\"op\": \"predict\", \"id\": true, \"adapter\": \"a\", \"rows\": [{}], \"deadline_ms\": -1.5}", "predict", "-1.5 is less than or equal to the minimum of 0 (at /deadline_ms)", "the doc does not match predict in the socket schema:\n  at /deadline_ms: -1.5 is less than or equal to the minimum of 0\n  at /id: True is not of type 'integer'")]
    [InlineData("decide_request", "{\"dispatch\": {\"input\": \"x\", \"interface\": \"detect\", \"router\": \"\"}}", null, "'' should be non-empty (at /dispatch/router)", "the doc does not match the decide_request schema:\n  at /: {'dispatch': {'input': 'x', 'interface': 'detect', 'router': ''}} is not valid under any of the given schemas\n    - Additional properties are not allowed ('dispatch' was unexpected)\n    - 'calls' is a required property\n    - '' should be non-empty\n    - Additional properties are not allowed ('dispatch' was unexpected)\n    - 'graph' is a required property\n    - 'input' is a required property\n    - 'params' is a required property")]
    [InlineData("decide_request", "{\"graph\": {\"id\": \"\"}, \"input\": 1, \"params\": {}}", null, "'' should be non-empty (at /graph/id)", "the doc does not match the decide_request schema:\n  at /: {'graph': {'id': ''}, 'input': 1, 'params': {}} is not valid under any of the given schemas\n    - Additional properties are not allowed ('graph', 'input', 'params' were unexpected)\n    - 'calls' is a required property\n    - Additional properties are not allowed ('graph', 'input', 'params' were unexpected)\n    - 'dispatch' is a required property\n    - {'id': ''} is not valid under any of the given schemas")]
    [InlineData("decide_request", "{\"calls\": [], \"dispatch\": {}}", null, "Additional properties are not allowed ('dispatch' was unexpected)", "the doc does not match the decide_request schema:\n  at /: {'calls': [], 'dispatch': {}} is not valid under any of the given schemas\n    - Additional properties are not allowed ('dispatch' was unexpected)\n    - [] should be non-empty\n    - Additional properties are not allowed ('calls' was unexpected)\n    - 'input' is a required property\n    - 'interface' is a required property\n    - Additional properties are not allowed ('calls', 'dispatch' were unexpected)\n    - 'graph' is a required property\n    - 'input' is a required property\n    - 'params' is a required property")]
    [InlineData("graph_result", "{}", null, "'nodes' is a required property", "the doc does not match the graph_result schema:\n  at /: 'nodes' is a required property\n  at /: 'outputs' is a required property\n  at /: 'stages' is a required property")]
    public void FirstProblemAndValidateMatchPythonOnMoreDocuments(string schema, string document, string? definition, string first, string? validate)
    {
        var doc = J(document);
        Assert.Equal(first, Contract.FirstProblem(schema, doc, definition));
        Assert.Equal(validate, Assert.Throws<ContractError>(() => Contract.Validate(schema, doc, "the doc", definition)).Message);
        Assert.False(Contract.IsValid(schema, doc, definition));
    }

    [Fact]
    public void ADocumentPythonAcceptsIsAccepted()
    {
        // Python's $ matches before one trailing newline, and its \d matches any Unicode decimal digit, a
        // supplementary-plane one (U+1D7CF) included
        foreach (var version in new[] { "1.0.0\n", "\u0661.\u0662.\u0663", "\ud835\udfcf.2.3" })
        {
            var graph = new JsonObject
            {
                ["graph"] = "g", ["version"] = version, ["input"] = "text", ["params"] = new JsonArray(),
                ["nodes"] = new JsonObject { ["a"] = new JsonObject { ["call"] = "spam.is_spam" } }, ["outputs"] = new JsonArray("a"),
            };
            Assert.True(Contract.IsValid("graph", graph));
            Contract.Validate("graph", graph, "the doc");
        }
    }

    // Each row is the list of errors Draft202012Validator(schema).iter_errors(document) yields, in order (message,
    // keyword, absolute path, and the alternative index of each error in its context), as jsonschema 4.26 gives them.
    [Theory]
    [InlineData("{\"oneOf\": [{\"type\": \"integer\"}, {\"type\": \"number\"}, {\"minimum\": 0}]}", "3", "[{\"message\": \"3 is valid under each of {'type': 'number'}, {'minimum': 0}, {'type': 'integer'}\", \"keyword\": \"oneOf\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"oneOf\": [{\"type\": \"integer\"}, {\"type\": \"number\"}]}", "\"x\"", "[{\"message\": \"'x' is not valid under any of the given schemas\", \"keyword\": \"oneOf\", \"path\": [], \"alternatives\": [0, 1]}]")]
    [InlineData("{\"type\": [\"string\", \"null\"]}", "1", "[{\"message\": \"1 is not of type 'string', 'null'\", \"keyword\": \"type\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"items\": false}", "[1]", "[{\"message\": \"Expected at most 0 items but found 1 extra: 1\", \"keyword\": \"items\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"items\": false}", "[1, 2]", "[{\"message\": \"Expected at most 0 items but found 2 extra: [1, 2]\", \"keyword\": \"items\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"const\": [1, {\"a\": true}]}", "[1.0, {\"a\": true}]", "[]")]
    [InlineData("{\"const\": [1, {\"a\": true}]}", "[1, {\"a\": 1}]", "[{\"message\": \"[1, {'a': True}] was expected\", \"keyword\": \"const\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"const\": 1}", "true", "[{\"message\": \"1 was expected\", \"keyword\": \"const\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"enum\": [1, \"a\", null]}", "false", "[{\"message\": \"False is not one of [1, 'a', None]\", \"keyword\": \"enum\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"enum\": [0]}", "false", "[{\"message\": \"False is not one of [0]\", \"keyword\": \"enum\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"enum\": [1.0]}", "1", "[]")]
    [InlineData("{\"maximum\": 1}", "1.0000001", "[{\"message\": \"1.0000001 is greater than the maximum of 1\", \"keyword\": \"maximum\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"minimum\": 9007199254740993}", "9007199254740992.0", "[{\"message\": \"9007199254740992.0 is less than the minimum of 9007199254740993\", \"keyword\": \"minimum\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"exclusiveMinimum\": 0}", "0.0", "[{\"message\": \"0.0 is less than or equal to the minimum of 0\", \"keyword\": \"exclusiveMinimum\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"minimum\": 0}", "true", "[]")]
    [InlineData("{\"minLength\": 2}", "\"\ud83d\ude00\"", "[{\"message\": \"'\ud83d\ude00' is too short\", \"keyword\": \"minLength\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"maxLength\": 0}", "\"a\"", "[{\"message\": \"'a' is expected to be empty\", \"keyword\": \"maxLength\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"maxLength\": 1}", "\"\ud83d\ude00\"", "[]")]
    [InlineData("{\"maxItems\": 0}", "[1]", "[{\"message\": \"[1] is expected to be empty\", \"keyword\": \"maxItems\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"maxItems\": 1}", "[1, 2]", "[{\"message\": \"[1, 2] is too long\", \"keyword\": \"maxItems\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"minItems\": 2}", "[1]", "[{\"message\": \"[1] is too short\", \"keyword\": \"minItems\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"minProperties\": 2}", "{\"a\": 1}", "[{\"message\": \"{'a': 1} does not have enough properties\", \"keyword\": \"minProperties\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"maxProperties\": 0}", "{\"a\": 1}", "[{\"message\": \"{'a': 1} is expected to be empty\", \"keyword\": \"maxProperties\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"maxProperties\": 1}", "{\"a\": 1, \"b\": 2}", "[{\"message\": \"{'a': 1, 'b': 2} has too many properties\", \"keyword\": \"maxProperties\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"uniqueItems\": true}", "[1, 1.0]", "[{\"message\": \"[1, 1.0] has non-unique elements\", \"keyword\": \"uniqueItems\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"uniqueItems\": true}", "[1, true]", "[]")]
    [InlineData("{\"uniqueItems\": true}", "[{\"a\": 1}, {\"a\": 1.0}]", "[{\"message\": \"[{'a': 1}, {'a': 1.0}] has non-unique elements\", \"keyword\": \"uniqueItems\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"uniqueItems\": true}", "[\"a\", \"b\", \"a\"]", "[{\"message\": \"['a', 'b', 'a'] has non-unique elements\", \"keyword\": \"uniqueItems\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"uniqueItems\": true}", "[[1], [1]]", "[{\"message\": \"[[1], [1]] has non-unique elements\", \"keyword\": \"uniqueItems\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"dependentRequired\": {\"a\": [\"b\", \"c\"], \"d\": [\"e\"]}}", "{\"a\": 1, \"c\": 2, \"d\": 3}", "[{\"message\": \"'b' is a dependency of 'a'\", \"keyword\": \"dependentRequired\", \"path\": [], \"alternatives\": []}, {\"message\": \"'e' is a dependency of 'd'\", \"keyword\": \"dependentRequired\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"propertyNames\": {\"pattern\": \"^[a-z]+$\"}}", "{\"A\": 1, \"b\": 2, \"C\": 3}", "[{\"message\": \"'A' does not match '^[a-z]+$'\", \"keyword\": \"pattern\", \"path\": [], \"alternatives\": []}, {\"message\": \"'C' does not match '^[a-z]+$'\", \"keyword\": \"pattern\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"not\": {\"type\": \"string\"}}", "\"x\"", "[{\"message\": \"'x' should not be valid under {'type': 'string'}\", \"keyword\": \"not\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"if\": {\"type\": \"string\"}, \"then\": {\"minLength\": 3}, \"else\": {\"type\": \"integer\"}}", "\"ab\"", "[{\"message\": \"'ab' is too short\", \"keyword\": \"minLength\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"if\": {\"type\": \"string\"}, \"then\": {\"minLength\": 3}, \"else\": {\"type\": \"integer\"}}", "1.5", "[{\"message\": \"1.5 is not of type 'integer'\", \"keyword\": \"type\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"allOf\": [{\"type\": \"string\"}, {\"minLength\": 5}]}", "4", "[{\"message\": \"4 is not of type 'string'\", \"keyword\": \"type\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"required\": [\"b\", \"a\"], \"properties\": {\"a\": {\"type\": \"string\"}}, \"type\": \"object\"}", "{\"a\": 1}", "[{\"message\": \"'b' is a required property\", \"keyword\": \"required\", \"path\": [], \"alternatives\": []}, {\"message\": \"1 is not of type 'string'\", \"keyword\": \"type\", \"path\": [\"a\"], \"alternatives\": []}]")]
    [InlineData("{\"additionalProperties\": false, \"properties\": {\"a\": {}}}", "{\"a\": 1, \"\u00e9\": 2, \"Z\": 3, \"\ud83d\ude00\": 4}", "[{\"message\": \"Additional properties are not allowed ('Z', '\u00e9', '\ud83d\ude00' were unexpected)\", \"keyword\": \"additionalProperties\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"pattern\": \"^(?![\\\\s\\\\S]*[\\\\u0020]$)\"}", "\"trailing \"", "[{\"message\": \"'trailing ' does not match '^(?![\\\\\\\\s\\\\\\\\S]*[\\\\\\\\u0020]$)'\", \"keyword\": \"pattern\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"type\": \"integer\"}", "1e+300", "[]")]
    [InlineData("{\"type\": \"integer\"}", "2.0", "[]")]
    [InlineData("{\"type\": \"number\"}", "false", "[{\"message\": \"False is not of type 'number'\", \"keyword\": \"type\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"enum\": [\"a\"]}", "\"it's\"", "[{\"message\": \"\\\"it's\\\" is not one of ['a']\", \"keyword\": \"enum\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"enum\": [\"a\"]}", "\"both ' and \\\"\"", "[{\"message\": \"'both \\\\' and \\\"' is not one of ['a']\", \"keyword\": \"enum\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"oneOf\": [{\"properties\": {\"a\": {\"oneOf\": [{\"type\": \"string\"}, {\"type\": \"array\"}]}}, \"required\": [\"a\"]}, {\"required\": [\"b\"]}]}", "{\"a\": 1}", "[{\"message\": \"{'a': 1} is not valid under any of the given schemas\", \"keyword\": \"oneOf\", \"path\": [], \"alternatives\": [0, 1]}]")]
    [InlineData("false", "1", "[{\"message\": \"False schema does not allow 1\", \"keyword\": null, \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"properties\": {\"a\": false}}", "{\"a\": [1]}", "[{\"message\": \"False schema does not allow [1]\", \"keyword\": null, \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"const\": 0.1}", "0.30000000000000004", "[{\"message\": \"0.1 was expected\", \"keyword\": \"const\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"enum\": [1e+16, 1e-05, 123.0]}", "2", "[{\"message\": \"2 is not one of [1e+16, 1e-05, 123.0]\", \"keyword\": \"enum\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"properties\": {\"x\": {\"properties\": {\"a\": false}}}}", "{\"x\": {\"a\": 1}}", "[{\"message\": \"False schema does not allow 1\", \"keyword\": null, \"path\": [\"x\"], \"alternatives\": []}]")]
    [InlineData("{\"pattern\": \"^\\\\d+\\\\.\\\\d+\\\\.\\\\d+$\"}", "\"\ud835\udfcf.2.3\"", "[]")]
    [InlineData("{\"pattern\": \"^\\\\d+\\\\.\\\\d+\\\\.\\\\d+$\"}", "\"\uff11.\uff12.\uff13\"", "[]")]
    [InlineData("{\"pattern\": \"^[a-z][a-z0-9_]*$\"}", "\"abc\\n\"", "[]")]
    [InlineData("{\"pattern\": \"^[a-z][a-z0-9_]*$\"}", "\"abc\\n\\n\"", "[{\"message\": \"'abc\\\\n\\\\n' does not match '^[a-z][a-z0-9_]*$'\", \"keyword\": \"pattern\", \"path\": [], \"alternatives\": []}]")]
    [InlineData("{\"pattern\": \"^(?![\\\\s\\\\S]*[\\\\u0000-\\\\u001f])\"}", "\"a\ud83d\ude00\\u001f\"", "[{\"message\": \"'a\ud83d\ude00\\\\x1f' does not match '^(?![\\\\\\\\s\\\\\\\\S]*[\\\\\\\\u0000-\\\\\\\\u001f])'\", \"keyword\": \"pattern\", \"path\": [], \"alternatives\": []}]")]
    public void TheValidatorYieldsPythonsErrorsInPythonsOrder(string schema, string document, string expected)
    {
        var errors = new SchemaRegistry([]).Errors(J(document), J(schema)!, "a test schema").Select(error => (JsonNode?)new JsonObject
        {
            ["message"] = error.Message,
            ["keyword"] = error.Keyword,
            ["path"] = new JsonArray(error.Path.Select(segment => segment is int index ? (JsonNode)(long)index : (string)segment).ToArray()),
            ["alternatives"] = new JsonArray(error.Context.Select(sub => (JsonNode?)(long)sub.Alternative!.Value).ToArray()),
        });
        JsonAssert.Equal(J(expected), new JsonArray(errors.ToArray()));
    }

    [Fact]
    public void ADeeplyNestedDocumentIsAContractErrorNotAStackOverflow()
    {
        static JsonObject Nested(int levels)
        {
            var condition = new JsonObject { ["node"] = "a", ["is"] = "x" };
            for (int level = 0; level < levels; level++) condition = new JsonObject { ["not"] = condition };
            return condition;
        }
        // Python checks a condition nested 164 levels deep and stops with a RecursionError past that
        Assert.True(Contract.IsValid("graph", Nested(150), "condition"));
        var error = Assert.Throws<ContractError>(() => Contract.IsValid("graph", Nested(100_000), "condition"));
        Assert.Equal("The document is nested too deeply to check against the schema (more than 512 levels of schema)", error.Message);
    }

    [Theory]
    [InlineData("^a.b$", ". (any character)")]
    [InlineData("^\\w+$", "\\w")]
    [InlineData("^[\\d]+$", "\\d inside a character class")]
    [InlineData("^x\\Z", "\\Z")]
    [InlineData("^x\\z", "\\z")]
    [InlineData("^a{,3}$", "{,n}")]
    public void APatternDotNetReadsDifferentlyIsRefused(string pattern, string construct)
    {
        var error = Assert.Throws<ContractError>(() => new SchemaRegistry([]).Errors(JsonValue.Create("x"), new JsonObject { ["pattern"] = pattern }, "x").ToList());
        Assert.Equal($"The schema pattern {PyFormat.Str(pattern)} uses {construct}, which .NET's regular expressions do not read as Python's re does", error.Message);
    }

    [Fact]
    public void APatternDotNetCannotParseIsAContractErrorNamingIt()
    {
        var error = Assert.Throws<ContractError>(() => new SchemaRegistry([]).Errors(JsonValue.Create("x"), new JsonObject { ["pattern"] = "(?P<name>x)" }, "x").ToList());
        Assert.StartsWith("The schema pattern '(?P<name>x)' is not one .NET's regular expressions can read: ", error.Message);
    }

    [Theory]
    [InlineData("#/allOf/-1")]
    [InlineData("#/allOf/ 0")]
    [InlineData("#/allOf/1")]
    public void AReferenceToNoArrayItemPointsAtNothing(string reference)
    {
        var schema = new JsonObject { ["$id"] = "https://example.test/s.json", ["allOf"] = new JsonArray(new JsonObject()) };
        var registry = new SchemaRegistry([schema]);
        var error = Assert.Throws<ContractError>(() => registry.Errors(JsonValue.Create(1), new JsonObject { ["$ref"] = reference }, "https://example.test/s.json"));
        Assert.StartsWith($"The schema reference {PyFormat.Str(reference)} points at nothing", error.Message);
    }
}
