from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers
from rest_framework.fields import CurrentUserDefault
from drf_extra_fields.fields import Base64ImageField
from djoser.serializers import UserSerializer as DjoserUserSerializer, UserCreateSerializer as DjoserUserCreateSerializer
import logging

from constants import (
    MIN_COOKING_TIME_VALUE,
    MIN_AMOUNT_VALUE
)

from recipes.models import (
    Ingredient,
    Recipe,
    RecipeIngredient,
    Favorite,
    ShoppingCart
)

from users.models import Subscription


User = get_user_model()
logger = logging.getLogger(__name__)


class UserSerializer(DjoserUserSerializer):
    """
    Сериализатор для отображения данных пользователей (только для чтения).
    Добавляет поле avatar.
    """
    avatar = serializers.SerializerMethodField(read_only=True)
    is_subscribed = serializers.SerializerMethodField(read_only=True)

    class Meta(DjoserUserSerializer.Meta):
        fields = DjoserUserSerializer.Meta.fields + ('avatar', 'is_subscribed',)
        read_only_fields = fields

    def get_avatar(self, obj):
        if not obj.avatar:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.avatar.url)
        return obj.avatar.url

    def get_is_subscribed(self, obj):
        request = self.context.get('request')
        return (
            request is not None
            and request.user.is_authenticated
            and request.user != obj
            and obj.subscribers.filter(user=request.user).exists()
        )


class AvatarSerializer(serializers.ModelSerializer):
    avatar = Base64ImageField(required=True, allow_null=False)
    class Meta:
        model = User
        fields = ('avatar',)


class IngredientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ('id', 'name', 'measurement_unit')
        read_only_fields = fields


class RecipeIngredientReadSerializer(serializers.ModelSerializer):
    id = serializers.ReadOnlyField(source='ingredient.id')
    name = serializers.ReadOnlyField(source='ingredient.name')
    measurement_unit = serializers.ReadOnlyField(source='ingredient.measurement_unit')
    class Meta:
        model = RecipeIngredient
        fields = ('id', 'name', 'measurement_unit', 'amount')
        read_only_fields = fields


class RecipeReadSerializer(serializers.ModelSerializer):
    author = UserSerializer(read_only=True)
    ingredients = RecipeIngredientReadSerializer(many=True, read_only=True, source='recipe_ingredients')
    is_favorited = serializers.SerializerMethodField(read_only=True)
    is_in_shopping_cart = serializers.SerializerMethodField(read_only=True)
    image = serializers.SerializerMethodField(read_only=True)
    class Meta:
        model = Recipe
        fields = (
            'id', 'author', 'ingredients', 'name', 'image', 'text',
            'cooking_time', 'is_favorited', 'is_in_shopping_cart'
        )
        read_only_fields = fields
    def _get_user_recipe_relation(self, obj, related_model):
        request = self.context.get('request')
        try:
            if not request or not hasattr(request, 'user') or not request.user.is_authenticated:
                return False
            return related_model.objects.filter(user=request.user, recipe=obj).exists()
        except Exception as e:
            logger.exception(f'Ошибка в _get_user_recipe_relation: {e}')
            return False
    def get_image(self, obj):
        if not obj.image:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url
    def get_is_favorited(self, obj):
        return self._get_user_recipe_relation(obj, Favorite)
    def get_is_in_shopping_cart(self, obj):
        return self._get_user_recipe_relation(obj, ShoppingCart)


class IngredientAmountWriteSerializer(serializers.Serializer):
    id = serializers.PrimaryKeyRelatedField(queryset=Ingredient.objects.all())
    amount = serializers.IntegerField(min_value=MIN_AMOUNT_VALUE)


class RecipeWriteSerializer(serializers.ModelSerializer):
    ingredients = IngredientAmountWriteSerializer(many=True, required=True)
    image = Base64ImageField(required=True, allow_null=False)
    author = serializers.HiddenField(default=CurrentUserDefault())
    cooking_time = serializers.IntegerField(min_value=MIN_COOKING_TIME_VALUE)
    class Meta:
        model = Recipe
        fields = ('id', 'author', 'ingredients', 'name', 'image', 'text', 'cooking_time')
    def validate_ingredients(self, ingredients):
        if not ingredients:
            raise serializers.ValidationError('Нужно указать хотя бы один ингредиент.')
        ingredient_ids = [item['id'] for item in ingredients]
        if len(ingredient_ids) != len(set(ingredient_ids)):
            raise serializers.ValidationError('Ингредиенты в рецепте не должны повторяться.')
        return ingredients
    def _create_ingredients(self, recipe, ingredients_data):
        RecipeIngredient.objects.bulk_create(
            RecipeIngredient(recipe=recipe, ingredient=ingredient_item['id'], amount=ingredient_item['amount'])
            for ingredient_item in ingredients_data
        )
    @transaction.atomic
    def create(self, validated_data):
        ingredients_data = validated_data.pop('ingredients')
        recipe = super().create(validated_data)
        self._create_ingredients(recipe, ingredients_data)
        return recipe
    @transaction.atomic
    def update(self, instance, validated_data):
        ingredients_data = validated_data.pop('ingredients', None)
        recipe = super().update(instance, validated_data)
        recipe.recipe_ingredients.all().delete()
        self._create_ingredients(recipe, ingredients_data)
        return recipe
    def validate(self, data):
        instance = getattr(self, 'instance', None)
        if instance and 'ingredients' not in data:
            raise serializers.ValidationError("Поле 'ingredients' обязательно при обновлении рецепта.")
        return data
    def to_representation(self, instance):
        return RecipeReadSerializer(instance, context=self.context).data


class RecipeShortSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField(read_only=True)
    class Meta:
        model = Recipe
        fields = ('id', 'name', 'image', 'cooking_time')
        read_only_fields = fields

    def get_image(self, obj):
        if not obj.image:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url


class AuthorWithRecipesSerializer(UserSerializer):
    """
    Сериализатор для отображения авторов, на которых подписан пользователь.
    Добавляет кол-во рецептов и их сокращенный список.
    """
    recipes_count = serializers.SerializerMethodField(read_only=True)
    recipes = serializers.SerializerMethodField(read_only=True)
    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ('recipes_count', 'recipes')
        read_only_fields = fields

    def get_recipes_count(self, obj):
        return obj.recipes.count()

    def get_recipes(self, obj):
        request = self.context.get('request')
        default_limit = 3
        limit_str = request.query_params.get('recipes_limit', default_limit)
        try:
            limit = int(limit_str)
            if limit < 0:
                limit = 0
        except (ValueError, TypeError):
            limit = default_limit
        recipes_queryset = obj.recipes.all()[:limit]
        serializer = RecipeShortSerializer(recipes_queryset, many=True, context=self.context)
        return serializer.data
