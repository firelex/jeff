// offline
import ai.jeff.sdk.*;

public class CheckInstall {
    public static void main(String[] args) {
        Catalogue catalogue = Catalogue.shipped();

        System.out.println(catalogue.manifests().size() + " adapters in the catalogue shipped with ai.jeff.sdk");
    }
}
