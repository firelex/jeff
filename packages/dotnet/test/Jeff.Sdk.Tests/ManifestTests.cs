using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class ManifestTests
{
    private static JsonObject SpamRaw() => (JsonObject)Json.Canonical(Catalogue.Shipped().Adapter("jeff.safety.spam").Raw)!;

    [Theory]
    [InlineData("channel", "channel")]
    [InlineData("counterparty name", "counterparty_name")]
    [InlineData("Deal ID", "deal_id")]
    [InlineData("outreach  log -- v2", "outreach_log_v2")]
    [InlineData("_odd_", "odd")]
    [InlineData("clause type?", "clause_type")]
    [InlineData("company_", "company_")]
    [InlineData("a__b", "a__b")]
    [InlineData("x9_", "x9_")]
    public void TheCodeNameOfAKeyFollowsThePythonRule(string key, string code) => Assert.Equal(code, Manifest.CodeNameOf(key));

    [Theory]
    [InlineData("jeff.example.router-v14-fast", "jeff.example.router-fast")]
    [InlineData("jeff.routing.router-open-v14", "jeff.routing.router-open")]
    [InlineData("jeff.v2.router-v3", "jeff.v2.router")]
    [InlineData("jeff.support.triage", null)]
    [InlineData("jeff.routing.v14", null)]
    [InlineData("jeff.routing.router-v14-v15", null)]
    [InlineData("jeff.routing.router-v", null)]
    [InlineData("jeff.routing.router-v1x", null)]
    [InlineData("jeff.routing.router-14", null)]
    [InlineData("jeff.routing.router-open-v14\n", null)]
    public void TheUnversionedIdRemovesTheOneVersionSegmentAsInPython(string adapterId, string? unversioned) =>
        Assert.Equal(unversioned, Manifest.UnversionedId(adapterId));

    [Fact]
    public void ReferencesParseAsInPython()
    {
        Assert.Equal(new FunctionRef("jeff.safety.spam", "is_spam", "1.3"), Ids.ParseRef("jeff.safety.spam.is_spam@1.3"));
        Assert.Equal(new FunctionRef("spam", null, null), Ids.ParseRef("spam"));
        Assert.Equal("Malformed reference 'jeff.x': use jeff.<domain>.<name>[.<function>]",
            Assert.Throws<InvalidReference>(() => Ids.ParseRef("jeff.x")).Message);
        Assert.True(Ids.VersionMatches("1.3", "1.3.0"));
        Assert.False(Ids.VersionMatches("1.30", "1.3.0"));
    }

    [Fact]
    public void FixedLabelKeysThatLookLikeNumbersKeepTheirYamlOrder()
    {
        var raw = SpamRaw();
        raw["functions"]!["message_type"]!["labels"]!["fixed"] = new JsonObject { ["2"] = "Two", ["1"] = "One", ["legit"] = "Fine" };
        var manifest = Manifest.FromDict(raw, "test");
        Assert.Equal(new[] { "2", "1", "legit" }, manifest.Functions["message_type"].Labels.Fixed.Keys);
    }

    [Fact]
    public void AKeyWithNoUsableCodeNameMustDeclareOne()
    {
        var raw = SpamRaw();
        raw["context"] = new JsonArray(new JsonObject { ["key"] = "2nd party", ["description"] = "A field.", ["required"] = true });
        var error = Assert.Throws<ManifestError>(() => Manifest.FromDict(raw, "test"));
        Assert.Matches("2nd party.*code_name", error.Message);
    }

    [Fact]
    public void TheCatalogueResolvesAliasesKeysAndVersions()
    {
        var catalogue = Catalogue.Shipped();
        Assert.Equal("jeff.safety.spam.is_spam", catalogue.Function("spam.is_spam").Id);
        Assert.Throws<UnknownFunction>(() => catalogue.Function("spam"));
        Assert.Equal(new[] { "jeff.compliance.sanctions.screening" }, catalogue.Implementing("equivalent").Select(fn => fn.Id));
        Assert.True(Manifest.CalibrationFor(catalogue.Function("spam.is_spam"), null)!.Threshold > 0);
        Assert.Same(catalogue, Catalogue.Shipped());
    }

    [Fact]
    public void TestIdPatternsMustEachBeARegularExpression()
    {
        var raw = SpamRaw();
        raw["tests"]!["function_by"] = "id_pattern";
        raw["tests"]!["id_patterns"] = new JsonObject { ["is_spam"] = "[", ["message_type"] = ".*" };
        var error = Assert.Throws<ManifestError>(() => Manifest.FromDict(raw, "test"));
        Assert.Matches("id_patterns is_spam.*not a regular expression", error.Message);
    }

    [Fact]
    public void BaseLooksUpAVersionOfBasesYaml()
    {
        var catalogue = Catalogue.Shipped();
        Assert.Equal("mstrasser/jeff-base", catalogue.Base("jeff-base", "v1.4")["repo"]!.GetValue<string>());
        Assert.StartsWith("Base jeff-base v9 is not in bases.yaml (known: {", Assert.Throws<ManifestError>(() => catalogue.Base("jeff-base", "v9")).Message);
    }

    [Fact]
    public void ACatalogueFromAFolderReadsItsManifestsInCodePointOrder()
    {
        var catalogue = Catalogue.FromDir(Repo.PathOf("catalogue"));
        Assert.Equal(Catalogue.Shipped().Manifests().Select(manifest => manifest.Id), catalogue.Manifests().Select(manifest => manifest.Id));
        Assert.EndsWith("is missing", Assert.Throws<ManifestError>(() => Catalogue.FromDir(Repo.PathOf("contract"))).Message);
    }

    /// <summary>A scratch catalogue folder holding bases.yaml and the named shipped adapters' folders.</summary>
    private static string ScratchCatalogue(params (string Folder, string Adapter)[] adapters)
    {
        string root = Directory.CreateTempSubdirectory("jeff-catalogue-").FullName;
        File.Copy(Repo.PathOf("catalogue", "bases.yaml"), Path.Combine(root, "bases.yaml"));
        foreach (var (folder, adapter) in adapters)
        {
            Directory.CreateDirectory(Path.Combine(root, folder));
            File.Copy(Repo.PathOf("catalogue", adapter, "manifest.yaml"), Path.Combine(root, folder, "manifest.yaml"));
        }
        return root;
    }

    [Fact]
    public void ACatalogueFolderIsReadInFolderNameOrderAsPythonsGlobIs()
    {
        // by folder name "a" comes before "a-b"; by full path ("a-b/manifest.yaml" against "a/manifest.yaml") it would not
        string root = ScratchCatalogue(("a-b", "jeff.safety.guard"), ("a", "jeff.safety.spam"), (".hidden", "jeff.support.triage"));
        try
        {
            File.WriteAllText(Path.Combine(root, "a-file"), "not a folder");
            Directory.CreateDirectory(Path.Combine(root, "empty"));
            var catalogue = Catalogue.FromDir(root);
            Assert.Equal(new[] { "jeff.support.triage", "jeff.safety.spam", "jeff.safety.guard" }, catalogue.Manifests().Select(manifest => manifest.Id));
            Assert.Equal(Path.Combine(root, "a", "manifest.yaml"), catalogue.Adapter("spam").Path);
        }
        finally
        {
            Directory.Delete(root, recursive: true);
        }
    }

    [Fact]
    public void ACatalogueFolderThatCannotBeReadIsAnError()
    {
        if (OperatingSystem.IsWindows()) return; // the folder is locked with a Unix file mode
        string root = ScratchCatalogue(("a", "jeff.safety.spam"), ("locked", "jeff.safety.guard"));
        string locked = Path.Combine(root, "locked");
        try
        {
            File.SetUnixFileMode(locked, UnixFileMode.None);
            var error = Assert.Throws<ManifestError>(() => Catalogue.FromDir(root));
            Assert.StartsWith($"{locked} cannot be read, so whether it holds an adapter's manifest.yaml is unknown (", error.Message);
        }
        finally
        {
            File.SetUnixFileMode(locked, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            Directory.Delete(root, recursive: true);
        }
    }

    [Fact]
    public void AManifestFileThatIsNotUtf8IsRefusedNotRepaired()
    {
        string root = ScratchCatalogue(("a", "jeff.safety.spam"));
        try
        {
            string file = Path.Combine(root, "a", "manifest.yaml");
            File.WriteAllBytes(file, [.. File.ReadAllBytes(file), 0x23, 0xFF, 0x0A]);
            Assert.Throws<System.Text.DecoderFallbackException>(() => Catalogue.FromDir(root));
        }
        finally
        {
            Directory.Delete(root, recursive: true);
        }
    }

    [Fact]
    public void LookupsPythonCrashesOnAreNamedErrors()
    {
        var catalogue = Catalogue.Shipped();
        var stray = catalogue.Function("spam.is_spam") with { AdapterId = "jeff.x.y" };
        Assert.Equal("No adapter 'jeff.x.y' in the catalogue", Assert.Throws<UnknownFunction>(() => catalogue.ManifestOf(stray)).Message);
        Assert.Same(catalogue.Adapter("spam"), catalogue.ManifestOf(catalogue.Function("spam.is_spam")));
        var empty = catalogue.Adapter("spam") with { Functions = new OrderedMap<FunctionSpec>() };
        var replaced = catalogue.Replacing([empty]);
        Assert.Equal("jeff.safety.spam has no functions", Assert.Throws<UnknownFunction>(() => replaced.Function("spam")).Message);
    }

    [Fact]
    public void AnIdPatternUsingAConstructTheCheckDoesNotFollowIsRefusedByName()
    {
        var raw = SpamRaw();
        raw["tests"]!["function_by"] = "id_pattern";
        raw["tests"]!["id_patterns"] = new JsonObject { ["is_spam"] = "(?i).+-spam", ["message_type"] = ".+" };
        Assert.Equal("test: tests.id_patterns is_spam: '(?i).+-spam' uses inline flags (?i...), which the .NET SDK does not check as Python's re "
            + "module would; write the pattern without it", Assert.Throws<ManifestError>(() => Manifest.FromDict(raw, "test")).Message);
    }

    [Fact]
    public void AChangeToASharedManifestsDocumentsIsNotSeenByAnotherCaller()
    {
        var first = Catalogue.Shipped().Adapter("spam");
        first.Raw["id"] = "changed";
        first.Tests["function_by"] = "changed";
        first.Weights["changed"] = true;
        first.Raw["tests"]!["function_by"] = "changed";
        var second = Catalogue.Shipped().Adapter("spam");
        Assert.Equal("jeff.safety.spam", second.Raw["id"]!.GetValue<string>());
        Assert.Equal("id_suffix", second.Tests["function_by"]!.GetValue<string>());
        Assert.Equal("id_suffix", second.Raw["tests"]!["function_by"]!.GetValue<string>());
        Assert.False(second.Weights.ContainsKey("changed"));
        Assert.NotSame(second.Raw, second.Raw);
        var built = first with { Raw = new JsonObject { ["id"] = "x" } };
        built.Raw["id"] = "y";
        Assert.Equal("x", built.Raw["id"]!.GetValue<string>());
    }

    [Fact]
    public void TheCodeNameRegexMatchesWholeKeysOnly()
    {
        Assert.DoesNotMatch(Manifest.CodeName, "abc\n");
        Assert.Matches(Manifest.CodeName, "abc");
    }
}

/// <summary>The cases of Data/python-catalogue.json: what the Python SDK did with each input, replayed here.</summary>
public class PythonRecordedCatalogueTests
{
    private static readonly Lazy<JsonObject> Recorded = new(() =>
        (JsonObject)Json.Parse(File.ReadAllText(Repo.PathOf("packages", "dotnet", "test", "Jeff.Sdk.Tests", "Data", "python-catalogue.json")),
            "Data/python-catalogue.json")!);

    private static IEnumerable<JsonObject> Items(string key) => Recorded.Value[key]!.AsArray().Select(item => item!.AsObject());

    public static TheoryData<string> ManifestCases() => new(Items("manifests").Select(item => item["name"]!.GetValue<string>()));

    public static TheoryData<int> PatternCases() => new(Enumerable.Range(0, Recorded.Value["patterns"]!.AsArray().Count));

    public static TheoryData<int> RefCases() => new(Enumerable.Range(0, Recorded.Value["refs"]!.AsArray().Count));

    private static void Apply(JsonObject raw, JsonArray ops)
    {
        foreach (var op in ops.Select(item => item!.AsArray()))
        {
            var path = op[1]!.AsArray().Select(part => part!.GetValue<string>()).ToList();
            JsonObject node = raw;
            foreach (var part in path[..^1]) node = node[part]!.AsObject();
            if (op[0]!.GetValue<string>() == "set") node[path[^1]] = Json.Canonical(op[2]);
            else Assert.True(node.Remove(path[^1]));
        }
    }

    private static void ExpectError(JsonNode expected, Action action)
    {
        var error = Assert.ThrowsAny<JeffError>(action);
        Assert.Equal(expected["type"]!.GetValue<string>(), error.Type);
        Assert.Equal(expected["message"]!.GetValue<string>(), error.Message);
    }

    [Theory]
    [MemberData(nameof(ManifestCases))]
    public void AManifestIsReadOrRefusedAsPythonDoes(string name)
    {
        var item = Items("manifests").Single(entry => entry["name"]!.GetValue<string>() == name);
        var raw = (JsonObject)Json.Canonical(Catalogue.Shipped().Adapter(item["adapter"]!.GetValue<string>()).Raw)!;
        Apply(raw, item["ops"]!.AsArray());
        var expect = item["expect"]!;
        if (expect["error"] is { } error)
        {
            ExpectError(error, () => Manifest.FromDict(raw, "test"));
            return;
        }
        var manifest = Manifest.FromDict(raw, "test");
        Assert.Equal(expect["ok"]!["functions"]!.AsArray().Select(key => key!.GetValue<string>()), manifest.Functions.Keys);
        var codes = manifest.Context.Select(field => new[] { field.Key, field.CodeName }).Append([manifest.Input.Key, manifest.Input.CodeName]);
        Assert.Equal(expect["ok"]!["codes"]!.AsArray().Select(pair => pair!.AsArray().Select(part => part!.GetValue<string>()).ToArray()), codes);
    }

    [Theory]
    [MemberData(nameof(PatternCases))]
    public void AnIdPatternIsAcceptedOrRefusedAsPythonsReDoes(int index)
    {
        var item = Recorded.Value["patterns"]![index]!;
        string pattern = item["pattern"]!.GetValue<string>();
        string python = item["python"]!.GetValue<string>();
        try
        {
            PythonReSyntax.Check(pattern);
            Assert.Equal("accepted", python);
        }
        catch (PythonReError error)
        {
            Assert.Contains(python, new[] { "refused", "OverflowError" });
            Assert.Equal(item["message"]!.GetValue<string>(), error.Message);
        }
        catch (PythonReUnchecked)
        {
            // a construct the check does not follow (see PythonReSyntax): only the recorded patterns that use one
            Assert.Contains(pattern, RefusedByName);
        }
    }

    /// <summary>The recorded patterns that use a construct PythonReSyntax refuses by name, whatever Python says.</summary>
    private static readonly string[] RefusedByName =
        ["\\N{EM DASH}", "\\N", "(?<=a)", "(?<!a)", "(?(1)a", "(?i)a", "a(?i)", "(?x) a", "(?-i:a)", "(?P<\u00e9>x)"];

    [Fact]
    public void EveryRecordedPatternThatUsesARefusedConstructIsRefusedByName()
    {
        var recorded = Recorded.Value["patterns"]!.AsArray().Select(item => item!["pattern"]!.GetValue<string>()).ToHashSet(StringComparer.Ordinal);
        foreach (var pattern in RefusedByName)
        {
            Assert.Contains(pattern, recorded);
            Assert.Throws<PythonReUnchecked>(() => PythonReSyntax.Check(pattern));
        }
    }

    [Theory]
    [InlineData("(?<=a)b", "a look-behind (?<=...)")]
    [InlineData("(?<!a)b", "a look-behind (?<!...)")]
    [InlineData("(a)(?(1)b|c)", "a conditional (?(...)...)")]
    [InlineData("(?i)a", "inline flags (?i...)")]
    [InlineData("(?-i:a)", "inline flags (?-...)")]
    [InlineData("(?x) a", "inline flags (?x...)")]
    [InlineData("\\N{EM DASH}", "\\N{...} (a character by its Unicode name)")]
    [InlineData("[\\N{EM DASH}]", "\\N{...} (a character by its Unicode name)")]
    [InlineData("(?P<\u00e9>x)", "the group name '\u00e9', which is not ASCII")]
    public void EachConstructTheCheckDoesNotFollowIsRefusedByName(string pattern, string construct) =>
        Assert.Equal(construct, Assert.Throws<PythonReUnchecked>(() => PythonReSyntax.Check(pattern)).Message);

    [Theory]
    [MemberData(nameof(RefCases))]
    public void AReferenceResolvesAsInPython(int index)
    {
        var item = Recorded.Value["refs"]![index]!;
        string reference = item["ref"]!.GetValue<string>();
        var catalogue = Catalogue.Shipped();
        Func<string> resolve = item["op"]!.GetValue<string>() == "function" ? () => catalogue.Function(reference).Id : () => catalogue.Adapter(reference).Id;
        if (item["expect"]!["error"] is { } error) ExpectError(error, () => resolve());
        else Assert.Equal(item["expect"]!["ok"]!.GetValue<string>(), resolve());
    }

    [Fact]
    public void CodeNamesVersionsAndBasesAreAsInPython()
    {
        foreach (var (key, code) in Recorded.Value["codes"]!.AsObject()) Assert.Equal(code!.GetValue<string>(), Manifest.CodeNameOf(key));
        foreach (var row in Items2("versions"))
        {
            Assert.Equal(row[2]!.GetValue<bool>(), Ids.VersionMatches(row[0]!.GetValue<string>(), row[1]!.GetValue<string>()));
        }
        var catalogue = Catalogue.Shipped();
        foreach (var item in Items("bases"))
        {
            string baseId = item["base"]!.GetValue<string>(), version = item["version"]!.GetValue<string>();
            if (item["expect"]!["error"] is { } error) ExpectError(error, () => catalogue.Base(baseId, version));
            else JsonAssert.Equal(item["expect"]!["ok"], catalogue.Base(baseId, version));
        }
        Assert.Equal(Items2("base_versions").Select(pair => (pair[0]!.GetValue<string>(), pair[1]!.GetValue<string>())), catalogue.BaseVersions());
        foreach (var (iface, ids) in Recorded.Value["implementing"]!.AsObject())
        {
            Assert.Equal(ids!.AsArray().Select(id => id!.GetValue<string>()), catalogue.Implementing(iface).Select(fn => fn.Id));
        }
        Assert.Equal(Recorded.Value["names"]!.AsArray().Select(name => name!.GetValue<string>()), PyFormat.Sorted(catalogue.Names()));
        Assert.Equal(Recorded.Value["order"]!.AsArray().Select(id => id!.GetValue<string>()), catalogue.Manifests().Select(manifest => manifest.Id));
    }

    private static IEnumerable<JsonArray> Items2(string key) => Recorded.Value[key]!.AsArray().Select(item => item!.AsArray());
}
